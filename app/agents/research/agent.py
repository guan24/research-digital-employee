from __future__ import annotations

import os
from pathlib import Path
from typing import Any

from app.agents.registry import AgentProfile, load_research_profile
from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.tools.research_tools import ResearchToolService


def build_research_agent(
    settings: Settings,
    db: Database,
    task: dict[str, Any],
    checkpointer: Any,
    started_monotonic: float,
):
    """Build one task-scoped Deep Agent. Imports are lazy so API/tests need not load model SDKs."""
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured; copy .env.example to .env and set the key")

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

    from deepagents import create_deep_agent
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
    from langchain_openai import ChatOpenAI

    # Keep the MVP deliberately single-agent and do not expose shell execution.
    register_harness_profile(
        "openai",
        HarnessProfile(
            general_purpose_subagent=GeneralPurposeSubagentProfile(enabled=False),
            excluded_tools=frozenset({"execute"}),
        ),
    )

    profile: AgentProfile = load_research_profile()
    artifacts = ArtifactStore(settings.workspace_dir, task["task_id"])
    tool_service = ResearchToolService(settings, db, task["task_id"], artifacts, started_monotonic,
        task.get("worker_id"), task.get("worker_generation"))
    model = ChatOpenAI(
        model=settings.openai_model,
        api_key=settings.openai_api_key,
        base_url=settings.openai_base_url,
        temperature=0.1,
        timeout=settings.model_timeout_seconds,
        max_retries=1,
    )
    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/research_memory/": FilesystemBackend(root_dir=str(artifacts.workspace), virtual_mode=True)},
    )
    summarization = SummarizationMiddleware(
        model=model, backend=backend, trigger=("fraction", 0.78), keep=("fraction", 0.18)
    )
    return create_deep_agent(
        model=model,
        tools=tool_service.as_langchain_tools(),
        system_prompt=profile.system_prompt + "\\n\\nTask memory: call read_research_memory at the start and after context compaction; save concise verified progress with save_research_memory before major phase transitions and before final synthesis. The task-scoped filesystem under /research_memory/ is durable across process restarts.",
        middleware=[summarization],
        checkpointer=checkpointer,
        subagents=[],
        backend=backend,
        name=profile.name,
        debug=False,
    )
