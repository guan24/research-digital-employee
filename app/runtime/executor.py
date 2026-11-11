from __future__ import annotations

import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from typing import Any, Callable, Iterator

from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.control import TaskPaused, TaskCancelled, TaskDeadlineExceeded, TaskStepLimitExceeded, WorkerLeaseLost
from app.runtime.budget import estimate_model_call_reservation
from app.runtime.errors import BudgetExhausted

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

    def __init__(self, db: Database, task_id: str, started_monotonic: float, max_runtime_seconds: int, worker_id: str | None = None, worker_generation: int | None = None):
        self.db = db
        self.task_id = task_id
        self.started_monotonic = started_monotonic
        self.max_runtime_seconds = max_runtime_seconds
        self.worker_id = worker_id
        self.worker_generation = worker_generation
        self._llm_started: dict[str, float] = {}
        self._tool_started: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}
        self._token_reservations: set[str] = set()

    def _check_control(self) -> None:
        task = self.db.get_task(self.task_id)
        if self.worker_id:
            if not self.db.owns_worker_lease(self.worker_id):
                raise WorkerLeaseLost("worker singleton lease was lost")
            if not task or task.get("worker_id") != self.worker_id:
                raise WorkerLeaseLost("task ownership changed")
            if self.worker_generation is not None and int(task.get("worker_generation") or 0) != int(self.worker_generation):
                raise WorkerLeaseLost("task execution generation changed")
        if task:
            self.db.emit_budget_alerts(self.task_id)
        flags = self.db.get_control_flags(self.task_id)
        if not flags:
            raise TaskCancelled("task record no longer exists")
        if flags["cancel_requested"] or flags["status"] == "cancelled":
            raise TaskCancelled("task cancellation requested")
        if flags["pause_requested"] or flags["status"] == "paused":
            raise TaskPaused("task pause requested")
        budget = self.db.check_runtime_budget(
            self.task_id, self.worker_id, self.worker_generation
        )
        if budget.get("exhausted"):
            raise TaskDeadlineExceeded(
                f"cumulative runtime budget exhausted: used={budget['used']:.2f}s "
                f"limit={budget['limit']}s"
            )

    def _reserve_model_tokens(self, run_id, raw_request):
        reservation_id = str(run_id)
        if reservation_id in self._token_reservations:
            return
        estimate = estimate_model_call_reservation(raw_request)
        result = self.db.reserve_tokens(
            self.task_id, reservation_id, estimate,
            self.worker_id, self.worker_generation,
        )
        if not result.get("allowed"):
            raise BudgetExhausted(
                "token budget exhausted before model call: "
                f"requested={result.get('requested', estimate)}, "
                f"used={result.get('used', 0)}, limit={result.get('limit', 0)}, "
                f"remaining={result.get('remaining', 0)}"
            )
        self._token_reservations.add(reservation_id)

    def _on_model_start(self, serialized, run_id, input_summary="", raw_request=None):
        self._check_control()
        self._reserve_model_tokens(run_id, raw_request if raw_request is not None else input_summary)
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
        total_tokens = usage.get("total_tokens") or usage.get("totalTokens")
        if total_tokens is None:
            total_tokens = (usage.get("prompt_tokens") or usage.get("input_tokens") or 0) + (usage.get("completion_tokens") or usage.get("output_tokens") or 0)
        try:
            total_tokens = max(0, int(total_tokens))
        except (TypeError, ValueError):
            total_tokens = 0
        reservation_id = str(run_id)
        self.db.settle_token_reservation(
            self.task_id, reservation_id, total_tokens,
            self.worker_id, self.worker_generation,
        )
        self._token_reservations.discard(reservation_id)
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
        self._check_control()

    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        reservation_id = str(run_id)
        self.db.release_token_reservation(self.task_id, reservation_id)
        self._token_reservations.discard(reservation_id)
        self.db.add_event(
            self.task_id,
            "model_call_failed",
            {"duration_ms": int((time.monotonic() - started) * 1000), "error": str(error)[:2000], "run_id": str(run_id)},
        )
        self._check_control()

    def on_tool_start(self, serialized, input_str, *, run_id, **kwargs):
        self._check_control()
        step = self.db.consume_execution_step(self.task_id, self.worker_id)
        if step is None:
            raise TaskStepLimitExceeded("task exceeded max_steps tool-call budget")
        tool_name = (serialized or {}).get("name", "unknown")
        self._tool_started[str(run_id)] = time.monotonic()
        self._tool_names[str(run_id)] = tool_name
        self.db.add_event(
            self.task_id,
            "tool_call_started",
            {"tool": tool_name, "input": str(input_str)[:2500], "run_id": str(run_id)},
        )

    def on_tool_end(self, output, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        tool_name = self._tool_names.pop(str(run_id), "unknown")
        self.db.add_event(
            self.task_id,
            "tool_call_completed",
            {"output": str(output)[:20000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )
        self._check_control()

    def on_tool_error(self, error, *, run_id, **kwargs):
        started = self._tool_started.pop(str(run_id), time.monotonic())
        tool_name = self._tool_names.pop(str(run_id), "unknown")
        self.db.add_event(
            self.task_id,
            "tool_call_failed",
            {"error": str(error)[:4000], "duration_ms": int((time.monotonic() - started) * 1000), "run_id": str(run_id)},
        )
        self._check_control()


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
    def __init__(self, settings: Settings, db: Database,
                 agent_builder: Callable[..., Any] | None = None,
                 checkpointer_factory: Callable[[Path], Any] = sqlite_checkpointer):
        self.settings = settings
        self.db = db
        self.agent_builder = agent_builder
        self.checkpointer_factory = checkpointer_factory

    def _registration(self, task):
        from app.agents.registry import get_agent_registration
        return get_agent_registration(task.get("agent_type", "research"))

    def _build_agent(self, task, checkpointer, started, registration):
        if self.agent_builder:
            return self.agent_builder(self.settings, self.db, task, checkpointer, started)
        return registration.builder(self.settings, self.db, task, checkpointer, started)

    def finalize_task(self, task_id: str, outcome: str) -> Any:
        task = self.db.get_task(task_id) or {}
        registration = self._registration(task)
        if registration.finalizer:
            artifacts = ArtifactStore(self.settings.workspace_dir, task_id)
            return registration.finalizer(self.db, task_id, artifacts, outcome, task)
        return None

    def finalize_plan(self, task_id: str, outcome: str) -> Any:
        # Backward-compatible alias; lifecycle is now dispatched by role.
        return self.finalize_task(task_id, outcome)

    @staticmethod
    def _validation_ok(result: Any) -> bool:
        if result is None:
            return True
        if isinstance(result, bool):
            return result
        return bool(getattr(result, "valid", True))

    @staticmethod
    def _validation_errors(result: Any) -> str:
        if result is None or isinstance(result, bool):
            return "delivery validation failed"
        errors = getattr(result, "errors", ())
        if isinstance(errors, str):
            return errors
        return "; ".join(str(item) for item in errors) or "delivery validation failed"

    def run(self, task: dict[str, Any], worker_id: str | None = None) -> str:
        from app.runtime.errors import DeliveryValidationExhausted
        task_id = task["task_id"]
        registration = self._registration(task)
        artifact_path = registration.output_artifact or "result.md"
        started = time.monotonic()
        artifacts = ArtifactStore(self.settings.workspace_dir, task_id)
        callback = TaskEventCallback(self.db, task_id, started,
            int(task["max_runtime_seconds"]), worker_id, task.get("worker_generation"))
        callback._check_control()
        config = {
            "configurable": {"thread_id": task_id},
            "recursion_limit": max(30, int(task["max_steps"]) * 5),
            "callbacks": [callback],
        }
        self.db.add_event(task_id, "agent_execution_started", {
            "thread_id": task_id, "agent_type": registration.agent_type,
            "output_artifact": artifact_path,
        })
        with self.checkpointer_factory(self.settings.checkpoint_path) as checkpointer:
            agent = self._build_agent(task, checkpointer, started, registration)
            snapshot = agent.get_state(config)
            if snapshot is not None and getattr(snapshot, "next", ()):
                self.db.add_event(task_id, "checkpoint_resume", {"pending_nodes": list(snapshot.next)})
                result = agent.invoke(None, config=config)
            elif snapshot is not None and getattr(snapshot, "values", None):
                self.db.add_event(task_id, "checkpoint_already_complete", {})
                result = snapshot.values
                if task.get("resume_mode"):
                    events = self.db.get_events(task_id, limit=5000)
                    failed_validation = next((event for event in reversed(events) if event["event_type"] == "delivery_validation_completed" and not event["payload"].get("valid")), None)
                    if failed_validation:
                        repair = "上次交付校验未通过，请基于已保存的报告和以下校验反馈进行定向修复，不要重新执行已完成的研究：\\n" + str(failed_validation["payload"].get("errors", "")) + "\\n修复后覆盖岗位声明的产物文件。"
                        self.db.add_event(task_id, "delivery_repair_resumed", {"source_event_id": failed_validation["event_id"]})
                        result = agent.invoke({"messages": [{"role": "user", "content": repair}]}, config=config)
            else:
                self.db.add_event(task_id, "agent_initial_run", {})
                result = agent.invoke({"messages": [{"role": "user", "content": task["goal"]}]}, config=config)

            if not artifacts.exists(artifact_path):
                final_text = extract_final_text(result)
                if not final_text.strip():
                    raise RuntimeError(f"agent finished without final answer or {artifact_path}")
                artifacts.write_text(artifact_path, final_text.strip() + "\n")
                self.db.add_event(task_id, "artifact_materialized_from_final_answer", {"path": artifact_path})
            else:
                self.db.add_event(task_id, "artifact_saved_by_agent", {"path": artifact_path})

            if registration.validator is not None:
                valid = False
                feedback = None
                for revision in range(registration.max_revisions + 1):
                    report = artifacts.read_text(artifact_path)
                    validation = registration.validator(report, task, artifacts)
                    valid = self._validation_ok(validation)
                    errors = self._validation_errors(validation)
                    self.db.add_event(task_id, "delivery_validation_completed", {
                        "agent_type": registration.agent_type, "valid": valid,
                        "errors": errors[:4000], "revision": revision,
                        "artifact_path": artifact_path,
                    })
                    if valid:
                        break
                    if revision >= registration.max_revisions:
                        raise DeliveryValidationExhausted(
                            f"delivery validation failed after {registration.max_revisions} revisions: {errors}"
                        )
                    if registration.postprocessor:
                        registration.postprocessor(agent, result, config, task, artifacts, validation)
                    else:
                        raise DeliveryValidationExhausted(
                            f"delivery validation failed and role has no repair handler: {errors}"
                        )
                if not valid:
                    raise DeliveryValidationExhausted("delivery validation did not pass")
        return str((artifacts.workspace / artifact_path).resolve())
