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



def _content_as_text(content: Any) -> str:
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, dict) and item.get("type") == "text":
                parts.append(str(item.get("text", "")))
            elif isinstance(item, str):
                parts.append(item)
        return "\n".join(part for part in parts if part)
    return json.dumps(content, ensure_ascii=False, default=str)


def _serialize_model_response(response: Any) -> dict[str, Any]:
    """Preserve assistant content and tool calls, including tool-only ChatOpenAI turns."""
    generations = getattr(response, "generations", None) or []
    serialized: list[dict[str, Any]] = []
    for group in generations:
        group_items = group if isinstance(group, (list, tuple)) else [group]
        for generation in group_items:
            message = getattr(generation, "message", None)
            if message is not None:
                item: dict[str, Any] = {
                    "content": getattr(message, "content", None) or ""
                }
                for attribute, key in (
                    ("tool_calls", "tool_calls"),
                    ("invalid_tool_calls", "invalid_tool_calls"),
                    ("additional_kwargs", "additional_kwargs"),
                ):
                    value = getattr(message, attribute, None)
                    if value:
                        item[key] = value
                if not item["content"] and not item.get("tool_calls") and not item.get("additional_kwargs"):
                    text_value = getattr(generation, "text", None)
                    if text_value:
                        item["text"] = text_value
                serialized.append(item)
            else:
                text_value = getattr(generation, "text", None)
                if text_value is not None:
                    serialized.append({"text": text_value})
    if serialized:
        return serialized[0] if len(serialized) == 1 else {"generations": serialized}

    for attribute in ("content", "text", "output"):
        value = getattr(response, attribute, None)
        if value not in (None, ""):
            return {attribute: value}
    model_dump = getattr(response, "model_dump", None)
    if callable(model_dump):
        try:
            return {"response": model_dump()}
        except Exception:
            pass
    return {"unparsed_response": repr(response)[:20_000]}


def _model_response_summary(raw_response: dict[str, Any]) -> str:
    content = _content_as_text(raw_response.get("content") or raw_response.get("text"))
    if content.strip():
        return content.strip()[:1200]
    calls = raw_response.get("tool_calls") or []
    if not calls and isinstance(raw_response.get("additional_kwargs"), dict):
        calls = raw_response["additional_kwargs"].get("tool_calls") or []
    names = []
    for call in calls:
        if isinstance(call, dict):
            function = call.get("function") or {}
            names.append(function.get("name") or call.get("name") or "tool")
    if names:
        return "模型决定调用工具：" + "、".join(names[:8])
    if raw_response.get("unparsed_response"):
        return "模型返回了结构化响应，已保留原始响应供查看。"
    return "模型已返回响应（无纯文本内容）。"


def _model_usage(response: Any) -> dict[str, Any]:
    llm_output = getattr(response, "llm_output", None) or {}
    if isinstance(llm_output, dict):
        usage = llm_output.get("token_usage") or llm_output.get("usage")
        if isinstance(usage, dict) and usage:
            return usage
    for group in getattr(response, "generations", None) or []:
        items = group if isinstance(group, (list, tuple)) else [group]
        for generation in items:
            message = getattr(generation, "message", None)
            if message is None:
                continue
            usage = getattr(message, "usage_metadata", None)
            if isinstance(usage, dict) and usage:
                return usage
            metadata = getattr(message, "response_metadata", None) or {}
            if isinstance(metadata, dict):
                usage = metadata.get("token_usage") or metadata.get("usage")
                if isinstance(usage, dict) and usage:
                    return usage
    return {}


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
        self._tool_names: dict[str, str] = {}

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
        raw_response = _serialize_model_response(response)
        usage = _model_usage(response)
        self.db.add_event(
            self.task_id,
            "model_call_completed",
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
                "decision_summary": _model_response_summary(raw_response),
                "raw_response": raw_response,
                "response_type": type(response).__name__,
                "run_id": str(run_id),
            },
        )

    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        self.db.add_event(
            self.task_id,
            "model_call_failed",
            {"duration_ms": int((time.monotonic() - started) * 1000), "error": str(error)[:2000], "run_id": str(run_id)},
        )

    def _record_plan_activity(self, tool_name: str, phase: str, run_id: str) -> None:
        if tool_name == "update_research_plan":
            return
        self.db.add_event(
            self.task_id,
            "research_plan_activity",
            {"tool": tool_name, "phase": phase, "run_id": run_id},
        )
        if phase != "started":
            return
        history = self.db.get_events(self.task_id, limit=2000)
        plan_events = [event for event in history if event["event_type"] == "research_plan_updated"]
        if not plan_events:
            return
        latest = plan_events[-1]
        plan = latest.get("payload", {}).get("plan") or {}
        steps = plan.get("steps") or []
        statuses = {str(step.get("id")): step.get("status", "pending") for step in steps}
        for event in history:
            if event["event_id"] <= latest["event_id"] or event["event_type"] != "research_plan_step_status":
                continue
            payload = event.get("payload", {})
            step_id = str(payload.get("step_id", ""))
            if step_id in statuses:
                statuses[step_id] = payload.get("status", statuses[step_id])
        if any(status == "in_progress" for status in statuses.values()):
            return
        next_step = next((step for step in steps if statuses.get(str(step.get("id"))) == "pending"), None)
        if next_step:
            self.db.add_event(
                self.task_id,
                "research_plan_step_status",
                {
                    "step_id": str(next_step["id"]),
                    "status": "in_progress",
                    "source": "system",
                    "reason": f"Observed tool execution: {tool_name}",
                    "tool": tool_name,
                },
            )

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._check_control()
        tool_name = (serialized or {}).get("name", "unknown")
        self._tool_started[str(run_id)] = time.monotonic()
        self._tool_names[str(run_id)] = tool_name
        self.db.add_event(
            self.task_id,
            "tool_call_started",
            {"tool": tool_name, "input": str(input_str)[:2500], "run_id": str(run_id)},
        )
        self._record_plan_activity(tool_name, "started", str(run_id))

    def on_tool_end(self, output, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        tool_name = self._tool_names.pop(str(run_id), "unknown")
        self.db.add_event(
            self.task_id,
            "tool_call_completed",
            {"output": str(output)[:20000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )
        self._record_plan_activity(tool_name, "completed", str(run_id))

    def on_tool_error(self, error, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        tool_name = self._tool_names.pop(str(run_id), "unknown")
        self.db.add_event(
            self.task_id,
            "tool_call_failed",
            {"error": str(error)[:4000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )
        self._record_plan_activity(tool_name, "failed", str(run_id))


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
