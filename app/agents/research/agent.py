from __future__ import annotations

import os
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.agents.registry import AgentProfile, load_research_profile
from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.tools.research_tools import ResearchToolService
from app.runtime.budget import MODEL_MAX_OUTPUT_TOKENS


DEFAULT_MODEL_MAX_INPUT_TOKENS = 128_000
KNOWN_MODEL_MAX_INPUT_TOKENS = {
    # Alibaba Cloud Model Studio documents 991,808 max input tokens for qwen3.8-flash.
    "qwen3.8-flash": 991_808,
}


def resolve_summarization_token_limits(
    model_name: str,
    configured_max_input_tokens: int = 0,
    model_profile: Mapping[str, Any] | None = None,
    task_max_total_tokens: int = 0,
) -> tuple[int, int]:
    """Bound context compaction by both the provider window and the task token budget."""
    max_input_tokens = int(configured_max_input_tokens or 0)
    if max_input_tokens <= 0 and isinstance(model_profile, Mapping):
        profile_limit = model_profile.get("max_input_tokens")
        if isinstance(profile_limit, int) and not isinstance(profile_limit, bool) and profile_limit > 0:
            max_input_tokens = profile_limit
    if max_input_tokens <= 0:
        normalized_name = str(model_name or "").strip().lower().rsplit("/", 1)[-1]
        for known_name, known_limit in KNOWN_MODEL_MAX_INPUT_TOKENS.items():
            if normalized_name == known_name or normalized_name.startswith(known_name + "-"):
                max_input_tokens = known_limit
                break
    if max_input_tokens <= 0:
        max_input_tokens = DEFAULT_MODEL_MAX_INPUT_TOKENS

    trigger_tokens = max(1, int(max_input_tokens * 0.78))
    keep_tokens = max(1, int(max_input_tokens * 0.18))
    task_budget = max(0, int(task_max_total_tokens or 0))
    if task_budget:
        trigger_tokens = min(trigger_tokens, max(512, int(task_budget * 0.22)))
        keep_tokens = min(keep_tokens, max(256, int(task_budget * 0.08)), max(1, trigger_tokens // 2))
    return trigger_tokens, keep_tokens

def build_research_agent(
    settings: Settings,
    db: Database,
    task: dict[str, Any],
    checkpointer: Any,
    started_monotonic: float,
    *, model: Any = None,
):
    """Build one task-scoped Deep Agent. Imports are lazy so API/tests need not load model SDKs."""
    if model is None and not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured; copy .env.example to .env and set the key")

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")

    from deepagents import create_deep_agent
    from deepagents.backends import CompositeBackend, FilesystemBackend, StateBackend
    from deepagents.middleware.summarization import SummarizationMiddleware
    from deepagents.profiles import GeneralPurposeSubagentProfile, HarnessProfile, register_harness_profile
    from langchain_openai import ChatOpenAI
    from app.runtime.tool_replay import tool_replay_guard
    from app.runtime.research_safety import model_output_guard

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
    if model is None:
        model = ChatOpenAI(
            model=settings.openai_model,
            api_key=settings.openai_api_key,
            base_url=settings.openai_base_url,
            temperature=0.1,
            timeout=settings.model_timeout_seconds,
            max_retries=1,
            max_tokens=MODEL_MAX_OUTPUT_TOKENS,
        )
    backend = CompositeBackend(
        default=StateBackend(),
        routes={"/research_memory/": FilesystemBackend(root_dir=str(artifacts.workspace), virtual_mode=True)},
    )
    trigger_tokens, keep_tokens = resolve_summarization_token_limits(
        settings.openai_model,
        getattr(settings, "model_max_input_tokens", 0),
        getattr(model, "profile", None),
        int(task.get("max_total_tokens") or 0),
    )
    summarization = SummarizationMiddleware(
        model=model,
        backend=backend,
        trigger=("tokens", trigger_tokens),
        keep=("tokens", keep_tokens),
    )
    return create_deep_agent(
        model=model,
        tools=tool_service.as_langchain_tools(),
        system_prompt=profile.system_prompt + "\\n\\nTask memory: call read_research_memory at the start and after context compaction; save concise verified progress with save_research_memory before major phase transitions and before final synthesis. The task-scoped filesystem under /research_memory/ is durable across process restarts.",
        middleware=[model_output_guard(db, task["task_id"]), tool_replay_guard(db, task["task_id"]), summarization],
        checkpointer=checkpointer,
        subagents=[],
        backend=backend,
        name=profile.name,
        debug=False,
    )
