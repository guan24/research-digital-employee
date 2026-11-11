from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Callable, Iterator

from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.tools.research_tools import TaskCancelled, TaskDeadlineExceeded, TaskPaused, WorkerLeaseLost

logger = logging.getLogger(__name__)


try:
    from langchain_core.callbacks import BaseCallbackHandler
except ImportError:  # Allows persistence/recovery unit tests without installing the optional agent stack.
    class BaseCallbackHandler:  # type: ignore[no-redef]
        pass


class TaskEventCallback(BaseCallbackHandler):
    """Record model/tool lifecycle events and cooperatively honor task controls."""

    raise_error = True

    def __init__(self, db: Database, task_id: str, started_monotonic: float, max_runtime_seconds: int, worker_id: str | None = None):
        self.db = db
        self.task_id = task_id
        self.started_monotonic = started_monotonic
        self.max_runtime_seconds = max_runtime_seconds
        self.worker_id = worker_id
        self._llm_started: dict[str, float] = {}
        self._tool_started: dict[str, float] = {}

    def _check_control(self) -> None:
        if self.worker_id and not self.db.owns_worker_lease(self.worker_id):
            raise WorkerLeaseLost("worker singleton lease was lost")
        if time.monotonic() - self.started_monotonic > self.max_runtime_seconds:
            raise TaskDeadlineExceeded("task exceeded max_runtime_seconds")
        flags = self.db.get_control_flags(self.task_id)
        if not flags:
            raise TaskCancelled("task record no longer exists")
        if flags["cancel_requested"] or flags["status"] == "cancelled":
            raise TaskCancelled("task cancellation requested")
        if flags["pause_requested"] or flags["status"] == "paused":
            raise TaskPaused("task pause requested")

    def _on_model_start(self, serialized, run_id, input_summary="", raw_request=None):
        self._check_control()
        self._llm_started[str(run_id)] = time.monotonic()
        model_name = (serialized or {}).get("name") or str((serialized or {}).get("id", ["unknown"])[-1])
        self.db.add_event(self.task_id, "model_call_started", {
            "model": model_name,
            "purpose": "research planning / evidence synthesis",
            "run_id": str(run_id),
            "input_summary": input_summary[:1200],
            "raw_request": raw_request,
        })

    def on_llm_start(self, serialized, prompts, *, run_id, **kwargs):
        self._on_model_start(serialized, run_id, "Text completion request",
                             {"prompt": [str(item)[:20000] for item in (prompts or [])]})

    def on_chat_model_start(self, serialized, messages, *, run_id, **kwargs):
        request_messages = []
        user_parts = []
        for batch in messages or []:
            for message in batch:
                role = getattr(message, "type", "unknown")
                content = getattr(message, "content", "")
                request_messages.append({"role": role, "content": content})
                if role in {"human", "user"}:
                    user_parts.append(str(content))
        self._on_model_start(serialized, run_id, " | ".join(user_parts)[-1200:],
                             {"messages": request_messages})

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        usage = {}
        try:
            generation = response.generations[0][0]
            message = generation.message
            usage = getattr(message, "usage_metadata", None) or getattr(message, "response_metadata", {}).get("token_usage", {}) or {}
        except Exception:
            pass
        output = ""
        try:
            output = str(response.generations[0][0].message.content)
        except Exception:
            pass
        self.db.add_event(
            self.task_id, "model_call_completed",
            {"duration_ms": int((time.monotonic() - started) * 1000),
             "usage": usage, "decision_summary": output[:1200], "raw_response": output[:20000], "run_id": str(run_id)},
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        self.db.add_event(
            self.task_id,
            "model_call_failed",
            {"duration_ms": int((time.monotonic() - started) * 1000), "error": str(error)[:2000], "run_id": str(run_id)},
        )

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._check_control()
        tool_name = (serialized or {}).get("name", "unknown")
        self._tool_started[str(run_id)] = time.monotonic()
        self.db.add_event(
            self.task_id,
            "tool_call_started",
            {"tool": tool_name, "input": str(input_str)[:2500], "run_id": str(run_id)},
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        self.db.add_event(
            self.task_id,
            "tool_call_completed",
            {"output": str(output)[:20000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )

    def on_tool_error(self, error, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        self.db.add_event(
            self.task_id,
            "tool_call_failed",
            {"error": str(error)[:4000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )


def extract_final_text(state_or_result: Any) -> str:
    if state_or_result is None:
        return ""
    if isinstance(state_or_result, dict):
        values = state_or_result
    else:
        values = getattr(state_or_result, "values", None)
    if not isinstance(values, dict):
        return ""
    messages = values.get("messages", [])
    for message in reversed(messages if isinstance(messages, list) else []):
        message_type = getattr(message, "type", None)
        content = getattr(message, "content", None)
        if content is None and isinstance(message, dict):
            message_type = message.get("type") or message.get("role")
            content = message.get("content")
        if message_type in {"ai", "assistant"} and content:
            if isinstance(content, str):
                return content
            if isinstance(content, list):
                text_parts = [item.get("text", "") for item in content if isinstance(item, dict) and item.get("type") == "text"]
                if text_parts:
                    return "\n".join(text_parts)
    return ""


@contextmanager
def sqlite_checkpointer(checkpoint_path: Path) -> Iterator[Any]:
    import os

    os.environ.setdefault("LANGGRAPH_STRICT_MSGPACK", "true")
    from langgraph.checkpoint.sqlite import SqliteSaver

    with SqliteSaver.from_conn_string(str(checkpoint_path)) as saver:
        yield saver


class ResearchExecutor:
    def __init__(
        self,
        settings: Settings,
        db: Database,
        agent_builder: Callable[..., Any] | None = None,
        checkpointer_factory: Callable[[Path], Any] = sqlite_checkpointer,
    ):
        self.settings = settings
        self.db = db
        self.agent_builder = agent_builder
        self.checkpointer_factory = checkpointer_factory

    def _build_agent(self, task: dict[str, Any], checkpointer: Any, started: float):
        if self.agent_builder:
            return self.agent_builder(self.settings, self.db, task, checkpointer, started)
        from app.agents.research.agent import build_research_agent

        return build_research_agent(self.settings, self.db, task, checkpointer, started)

    def run(self, task: dict[str, Any], worker_id: str | None = None) -> str:
        task_id = task["task_id"]
        started = time.monotonic()
        artifacts = ArtifactStore(self.settings.workspace_dir, task_id)
        callback = TaskEventCallback(self.db, task_id, started, int(task["max_runtime_seconds"]), worker_id)
        config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": max(30, int(task["max_steps"]) * 5),
            "callbacks": [callback],
        }
        self.db.add_event(task_id, "agent_execution_started", {"thread_id": task_id})
        self.db.add_event(task_id, "agent_plan_started", {"goal": task["goal"], "note": "Agent-generated plan and plan revisions are observable in subsequent model outputs and artifact saves."})

        with self.checkpointer_factory(self.settings.checkpoint_path) as checkpointer:
            agent = self._build_agent(task, checkpointer, started)
            snapshot = agent.get_state(config)
            if snapshot is not None and getattr(snapshot, "next", ()):
                self.db.add_event(task_id, "checkpoint_resume", {"pending_nodes": list(snapshot.next)})
                result = agent.invoke(None, config=config)
            elif snapshot is not None and getattr(snapshot, "values", None):
                # The graph finished but the worker may have died before updating the task row.
                self.db.add_event(task_id, "checkpoint_already_complete", {})
                result = snapshot.values
            else:
                self.db.add_event(task_id, "agent_initial_run", {})
                result = agent.invoke(
                    {"messages": [{"role": "user", "content": task["goal"]}]},
                    config=config,
                )

        report_path = "report.md"
        if not artifacts.exists(report_path):
            final_text = extract_final_text(result)
            if not final_text.strip():
                raise RuntimeError("agent finished without a final answer or report.md")
            artifacts.write_text(report_path, final_text.strip() + "\n")
            self.db.add_event(task_id, "report_materialized_from_final_answer", {"path": report_path})
        else:
            self.db.add_event(task_id, "report_saved_by_agent", {"path": report_path})
        return str((artifacts.workspace / report_path).resolve())
