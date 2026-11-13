from __future__ import annotations

import hashlib
import json
import logging
import time
from contextlib import contextmanager
from pathlib import Path
from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any, Callable, Iterator

from app.config import Settings
from app.infrastructure.artifact_store import ArtifactStore
from app.infrastructure.database import Database
from app.infrastructure.evidence_registry import EvidenceRegistry
from app.runtime.control import TaskPaused, TaskCancelled, TaskDeadlineExceeded, TaskStepLimitExceeded, WorkerLeaseLost, TaskWaitingForHuman
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
    """Preserve text, valid/invalid tool calls and provider finish diagnostics."""
    generations = getattr(response, "generations", None) or []
    serialized: list[dict[str, Any]] = []
    for group in generations:
        group_items = group if isinstance(group, (list, tuple)) else [group]
        for generation in group_items:
            message = getattr(generation, "message", None)
            if message is not None:
                item: dict[str, Any] = {"content": getattr(message, "content", None) or ""}
                for attribute, key in (
                    ("tool_calls", "tool_calls"),
                    ("invalid_tool_calls", "invalid_tool_calls"),
                ):
                    value = getattr(message, attribute, None)
                    if value:
                        item[key] = value
                additional = getattr(message, "additional_kwargs", None) or {}
                if isinstance(additional, dict):
                    cleaned = {key: value for key, value in additional.items() if value is not None}
                    if cleaned:
                        item["additional_kwargs"] = cleaned
                metadata = getattr(message, "response_metadata", None) or {}
                if isinstance(metadata, dict):
                    diagnostic = {
                        key: metadata[key]
                        for key in ("finish_reason", "model_name", "system_fingerprint", "refusal")
                        if metadata.get(key) is not None
                    }
                    if diagnostic:
                        item["response_metadata"] = diagnostic
                generation_info = getattr(generation, "generation_info", None)
                if isinstance(generation_info, dict) and generation_info:
                    item["generation_info"] = generation_info
                if not item["content"] and not item.get("tool_calls") and not item.get("invalid_tool_calls"):
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


def _response_items(raw_response: dict[str, Any]) -> list[dict[str, Any]]:
    generations = raw_response.get("generations")
    if isinstance(generations, list):
        return [item for item in generations if isinstance(item, dict)]
    return [raw_response]


def _model_response_summary(raw_response: dict[str, Any]) -> str:
    items = _response_items(raw_response)
    for item in items:
        content = _content_as_text(item.get("content") or item.get("text"))
        if content.strip():
            return content.strip()[:1200]

    invalid_calls = [
        call for item in items for call in (item.get("invalid_tool_calls") or [])
        if isinstance(call, dict)
    ]
    if invalid_calls:
        details = []
        for call in invalid_calls[:5]:
            name = call.get("name") or (call.get("function") or {}).get("name") or "unknown_tool"
            error = str(call.get("error") or call.get("args") or "")[:180]
            details.append(f"{name}: {error}" if error else str(name))
        return "模型生成了无法解析的工具调用：" + "；".join(details)

    calls = []
    for item in items:
        current = item.get("tool_calls") or []
        if not current and isinstance(item.get("additional_kwargs"), dict):
            current = item["additional_kwargs"].get("tool_calls") or []
        calls.extend(call for call in current if isinstance(call, dict))
    names = []
    for call in calls:
        function = call.get("function") or {}
        names.append(function.get("name") or call.get("name") or "tool")
    if names:
        return "模型请求调用工具：" + "、".join(names[:8])

    for item in items:
        additional = item.get("additional_kwargs") or {}
        refusal = additional.get("refusal") if isinstance(additional, dict) else None
        if refusal:
            return "模型返回拒绝响应：" + str(refusal)[:500]
        metadata = item.get("response_metadata") or {}
        finish_reason = metadata.get("finish_reason") or (item.get("generation_info") or {}).get("finish_reason")
        if finish_reason == "length":
            return "模型输出达到 max_tokens 上限，响应可能被截断；请缩短上下文或增加输出配额。"
    if raw_response.get("unparsed_response"):
        return "模型返回了未解析的结构化响应，已保留原始内容供诊断。"
    return "模型返回空响应：没有正文、有效工具调用或可识别的结构化输出。"


def _model_response_kind(raw_response: dict[str, Any]) -> str:
    items = _response_items(raw_response)
    if any(_content_as_text(item.get("content") or item.get("text")).strip() for item in items):
        return "text"
    if any(item.get("invalid_tool_calls") for item in items):
        return "invalid_tool_calls"
    if any(item.get("tool_calls") for item in items):
        return "tool_calls"
    if raw_response.get("unparsed_response") or raw_response.get("response"):
        return "structured"
    return "empty"

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

    def __init__(
        self, db: Database, task_id: str, started_monotonic: float,
        max_runtime_seconds: int, worker_id: str | None = None,
        worker_generation: int | None = None, plan_progress_service: Any | None = None,
        recovery_manager: Any | None = None, lifecycle_coordinator: Any | None = None,
    ):
        self.db = db
        self.task_id = task_id
        self.started_monotonic = started_monotonic
        self.max_runtime_seconds = max_runtime_seconds
        self.worker_id = worker_id
        self.worker_generation = worker_generation
        self.plan_progress_service = plan_progress_service
        self.recovery_manager = recovery_manager
        self.lifecycle_coordinator = lifecycle_coordinator
        self._llm_started: dict[str, float] = {}
        self._tool_started: dict[str, float] = {}
        self._tool_names: dict[str, str] = {}
        self._tool_inputs: dict[str, str] = {}
        self._tool_call_ids: dict[str, str] = {}
        self._tool_plan_context: dict[str, dict[str, Any] | None] = {}
        self._last_lifecycle_phase: str | None = None
        self._token_reservations: set[str] = set()

    def _check_control(self) -> None:
        task = self.db.get_task(self.task_id)
        if task and task.get("status") == "waiting_for_human":
            raise TaskWaitingForHuman("task is waiting for an operator decision")
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
                             {"prompt": [str(item)[:20000] for item in (prompts or [])],
                              "callback_kwargs": kwargs})

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
                             {"messages": request_messages, "callback_kwargs": kwargs})

    def on_llm_end(self, response, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        raw_response = _serialize_model_response(response)
        usage = _model_usage(response)
        total_tokens = usage.get("total_tokens") or usage.get("totalTokens")
        if total_tokens is None:
            total_tokens = (
                (usage.get("prompt_tokens") or usage.get("input_tokens") or 0)
                + (usage.get("completion_tokens") or usage.get("output_tokens") or 0)
            )
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
        summary = _model_response_summary(raw_response)
        response_kind = _model_response_kind(raw_response)
        self.db.add_event(
            self.task_id,
            "model_call_completed",
            {
                "duration_ms": int((time.monotonic() - started) * 1000),
                "usage": usage,
                "decision_summary": summary,
                "response_kind": response_kind,
                "raw_response": raw_response,
                "response_type": type(response).__name__,
                "run_id": str(run_id),
            },
        )
        if response_kind in {"empty", "invalid_tool_calls"}:
            self.db.add_event(
                self.task_id,
                "model_response_anomaly",
                {"kind": response_kind, "summary": summary[:1200], "run_id": str(run_id), "usage": usage},
            )
        self._check_control()
    def on_llm_error(self, error, *, run_id, **kwargs):
        started = self._llm_started.pop(str(run_id), time.monotonic())
        reservation_id = str(run_id)
        self.db.release_token_reservation(
            self.task_id, reservation_id, self.worker_id, self.worker_generation
        )
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
        self._tool_inputs[str(run_id)] = str(input_str)
        call_id = str(run_id)
        supplied_metadata = kwargs.get("metadata") or {}
        tool_metadata = dict(supplied_metadata) if isinstance(supplied_metadata, dict) else {}
        step_context = None
        if self.plan_progress_service is not None:
            try:
                step_context = self.plan_progress_service.sync_plan_progress_for_tool(
                    tool_name, str(input_str), tool_metadata,
                )
            except (TaskPaused, TaskCancelled, TaskDeadlineExceeded, WorkerLeaseLost):
                raise
            except Exception as exc:
                self.db.add_event(self.task_id, "research_plan_progress_sync_failed", {
                    "tool": tool_name, "error_type": type(exc).__name__, "error": str(exc)[:1000],
                })
        self._tool_plan_context[call_id] = step_context
        if step_context:
            tool_metadata.update({
                "plan_step_id": step_context.get("step_id"),
                "plan_version": step_context.get("plan_version"),
                "plan_step_phase": step_context.get("phase"),
                "plan_step_source": step_context.get("source"),
            })
        if self.recovery_manager is not None:
            call_id = self.recovery_manager.begin_tool_call(
                self.task_id, call_id, tool_name, str(input_str),
                int(self.worker_generation or 0), tool_metadata,
            )
        self._tool_call_ids[str(run_id)] = call_id
        event_payload = {
            "tool": tool_name, "input": str(input_str)[:2500], "run_id": str(run_id),
            "call_id": call_id, "recovery_tracking": self.recovery_manager is not None,
        }
        if step_context:
            event_payload.update({
                "plan_step_id": step_context.get("step_id"),
                "plan_version": step_context.get("plan_version"),
                "plan_step_phase": step_context.get("phase"),
                "plan_step_source": step_context.get("source"),
            })
        self.db.add_event(self.task_id, "tool_call_started", event_payload)

    def sync_plan_progress_for_phase(self, phase: str, note: str = "") -> None:
        if self.plan_progress_service is None:
            return
        try:
            self.plan_progress_service.sync_plan_progress_for_phase(phase, note)
        except (TaskPaused, TaskCancelled, TaskDeadlineExceeded, WorkerLeaseLost):
            raise
        except Exception as exc:
            self.db.add_event(self.task_id, "research_plan_progress_sync_failed", {
                "phase": phase, "error_type": type(exc).__name__, "error": str(exc)[:1000],
            })

    def complete_active_plan_step(self, note: str, output_path: str | None = None) -> None:
        if self.plan_progress_service is None:
            return
        try:
            self.plan_progress_service.complete_active_plan_step(note, output_path=output_path)
        except (TaskPaused, TaskCancelled, TaskDeadlineExceeded, WorkerLeaseLost):
            raise
        except Exception as exc:
            self.db.add_event(self.task_id, "research_plan_progress_sync_failed", {
                "phase": "completion", "error_type": type(exc).__name__, "error": str(exc)[:1000],
            })

    def _observe_research_phase(self, tool_name: str, input_value: str) -> None:
        if self.lifecycle_coordinator is None or self.plan_progress_service is None:
            return
        phase = self.plan_progress_service._tool_phase(tool_name, input_value)
        if phase is None or phase == self._last_lifecycle_phase:
            return
        try:
            result = self.lifecycle_coordinator.assess_progress(trigger="phase_boundary")
            decision = result.get("decision") or {}
            self.db.add_event(self.task_id, "research_phase_boundary_observed", {
                "phase": phase,
                "coverage_ratio": result.get("coverage", {}).get("coverage_ratio"),
                "recommended_action": decision.get("recommended_action"),
            })
            self._last_lifecycle_phase = phase
            if decision.get("recommended_action") == "REQUEST_HUMAN":
                intervention_id = self.db.set_waiting_for_human(
                    self.task_id, "research_control_requires_human",
                    {
                        "message": decision.get("reason") or "研究控制器检测到持续性关键缺口，需要人工决定研究方向。",
                        "coverage": result.get("coverage"),
                        "decision": decision,
                        "choices": [
                            "continue_research", "change_research_direction",
                            "add_research_requirements", "terminate",
                        ],
                    },
                    worker_id=self.worker_id,
                )
                if not intervention_id:
                    raise RuntimeError("research controller requested human review but task could not be parked")
                self.db.add_event(self.task_id, "research_controller_parked_for_human", {
                    "intervention_id": intervention_id, "phase": phase,
                    "recommended_action": "REQUEST_HUMAN",
                })
                raise TaskWaitingForHuman(
                    f"research controller parked task for human decision ({intervention_id})"
                )
        except (TaskPaused, TaskCancelled, TaskDeadlineExceeded, WorkerLeaseLost, TaskWaitingForHuman):
            raise
        except Exception as exc:
            self.db.add_event(self.task_id, "research_phase_assessment_failed", {
                "phase": phase, "error_type": type(exc).__name__, "error": str(exc)[:1000],
            })

    @staticmethod
    def _structured_tool_failure(output: Any) -> dict[str, Any] | None:
        output = getattr(output, "content", output)
        try:
            payload = json.loads(output) if isinstance(output, str) else output
        except (TypeError, json.JSONDecodeError):
            return None
        return payload if isinstance(payload, dict) and payload.get("success") is False else None

    @staticmethod
    def _step_event_context(context):
        if not context:
            return {}
        return {"plan_step_id": context.get("step_id"), "plan_version": context.get("plan_version"),
                "plan_step_phase": context.get("phase"), "plan_step_source": context.get("source")}

    def on_tool_end(self, output, *, run_id, **kwargs):
        self._check_tool_ownership()
        key = str(run_id)
        call_id = self._tool_call_ids.pop(key, key)
        started = self._tool_started.pop(key, time.monotonic())
        tool_name = self._tool_names.pop(key, "unknown")
        input_value = self._tool_inputs.pop(key, "")
        step_context = self._tool_plan_context.pop(key, None)
        failure = self._structured_tool_failure(output)
        if failure:
            if self.recovery_manager is not None:
                self.recovery_manager.fail_tool_call(self.task_id, call_id, output)
            if self.plan_progress_service is not None:
                self.plan_progress_service.record_tool_failure(
                    step_context, tool_call_id=call_id, tool_name=tool_name,
                    error=str(failure.get("error") or failure.get("message") or failure.get("error_type") or "structured tool failure"),
                    retryable=bool(failure.get("retryable", False)),
                )
            self.db.add_event(self.task_id, "tool_call_failed", {
                "output": str(output)[:20000], "failure_mode": "structured_result",
                "retryable": bool(failure.get("retryable", False)),
                "duration_ms": int((time.monotonic() - started) * 1000),
                "run_id": key, "call_id": call_id, "tool": tool_name,
                **self._step_event_context(step_context),
            })
        else:
            if self.recovery_manager is not None:
                self.recovery_manager.finish_tool_call(self.task_id, call_id, output)
            self.db.add_event(self.task_id, "tool_call_completed", {
                "output": str(output)[:20000], "duration_ms": int((time.monotonic() - started) * 1000),
                "run_id": key, "call_id": call_id, "tool": tool_name,
                **self._step_event_context(step_context),
            })
        self._observe_research_phase(tool_name, input_value)
        self._check_control()

    def on_tool_error(self, error, *, run_id, **kwargs):
        self._check_tool_ownership()
        key = str(run_id)
        call_id = self._tool_call_ids.pop(key, key)
        started = self._tool_started.pop(key, time.monotonic())
        tool_name = self._tool_names.pop(key, "unknown")
        self._tool_inputs.pop(key, None)
        step_context = self._tool_plan_context.pop(key, None)
        if self.recovery_manager is not None:
            self.recovery_manager.fail_tool_call(self.task_id, call_id, error)
        if self.plan_progress_service is not None:
            self.plan_progress_service.record_tool_failure(
                step_context, tool_call_id=call_id, tool_name=tool_name,
                error=str(error), retryable=True,
            )
        self.db.add_event(self.task_id, "tool_call_failed", {
            "error": str(error)[:4000], "duration_ms": int((time.monotonic() - started) * 1000),
            "run_id": key, "call_id": call_id, "tool": tool_name,
            **self._step_event_context(step_context),
        })
        self._check_control()

    def _check_tool_ownership(self) -> None:
        task = self.db.get_task(self.task_id)
        if self.worker_generation is not None and (
            not task or int(task.get("worker_generation") or 0) != self.worker_generation
        ):
            raise WorkerLeaseLost("task execution generation changed")
        if self.worker_id and (
            not task or task.get("worker_id") != self.worker_id
            or not self.db.owns_worker_lease(self.worker_id)
        ):
            raise WorkerLeaseLost("task ownership changed")


def extract_final_text(state_or_result: Any) -> str:
    """Extract only the final assistant turn; never substitute an earlier progress sentence."""
    if state_or_result is None:
        return ""
    values = state_or_result if isinstance(state_or_result, dict) else getattr(state_or_result, "values", None)
    if not isinstance(values, dict):
        return ""
    messages = values.get("messages", [])
    assistant_messages = []
    for message in messages if isinstance(messages, list) else []:
        message_type = getattr(message, "type", None)
        content = getattr(message, "content", None)
        if isinstance(message, dict):
            message_type = message.get("type") or message.get("role")
            content = message.get("content")
        if message_type in {"ai", "assistant"}:
            assistant_messages.append(content)
    if not assistant_messages:
        return ""
    content = assistant_messages[-1]
    if isinstance(content, str):
        return content.strip()
    if isinstance(content, list):
        parts = []
        for item in content:
            if isinstance(item, str):
                parts.append(item)
            elif isinstance(item, dict) and item.get("type") in {"text", "output_text"}:
                parts.append(str(item.get("text") or item.get("content") or ""))
        return "\n".join(part for part in parts if part).strip()
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
                 checkpointer_factory: Callable[[Path], Any] = sqlite_checkpointer,
                 research_reviewer: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
                 quality_review_enabled: bool = True):
        self.settings = settings
        self.db = db
        self.agent_builder = agent_builder
        self.checkpointer_factory = checkpointer_factory
        self.research_reviewer = research_reviewer
        self.quality_review_enabled = bool(quality_review_enabled)

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

    def pause_plan_steps(self, task_id: str) -> int:
        from app.runtime.plan_progress import PlanProgressTracker
        artifacts = ArtifactStore(self.settings.workspace_dir, task_id)
        return PlanProgressTracker(self.db, task_id, artifacts).pause_active_steps()

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

    def _verify_artifact(self, task_id: str, artifacts: ArtifactStore, path: str, source: str) -> str:
        content = artifacts.read_text(path)
        if not content.strip():
            raise ValueError(f"artifact is empty after persistence: {path}")
        digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
        self.db.add_event(task_id, "artifact_persisted_verified", {
            "path": path, "bytes": len(content.encode("utf-8")),
            "sha256": digest, "source": source, "read_back_verified": True,
        })
        return content

    def _persist_artifact(
        self, task_id: str, artifacts: ArtifactStore, path: str, content: str, source: str,
    ) -> str:
        normalized = str(content or "").strip()
        if not normalized:
            raise ValueError(f"cannot persist empty artifact content: {path}")
        artifacts.write_text(path, normalized + "\n")
        return self._verify_artifact(task_id, artifacts, path, source)

    def _park_for_human(self, task_id: str, worker_id: str | None,
                        category: str, request: dict[str, Any]) -> None:
        intervention_id = self.db.set_waiting_for_human(
            task_id, category, request, worker_id=worker_id
        )
        if not intervention_id:
            raise RuntimeError("could not atomically park task for human intervention")
        raise TaskWaitingForHuman(
            f"task parked for human decision: {category} ({intervention_id})"
        )

    def run(self, task: dict[str, Any], worker_id: str | None = None) -> str:
        from app.runtime.errors import DeliveryValidationExhausted
        task_id = task["task_id"]
        registration = self._registration(task)
        artifact_path = registration.output_artifact or "result.md"
        started = time.monotonic()
        artifacts = ArtifactStore(self.settings.workspace_dir, task_id)
        plan_progress_service = None
        recovery_manager = None
        lifecycle_coordinator = None
        if registration.agent_type == "research":
            from app.runtime.plan_progress import PlanProgressTracker
            from app.runtime.recovery_manager import RecoveryManager
            plan_progress_service = PlanProgressTracker(self.db, task_id, artifacts)
        from app.runtime.recovery_manager import RecoveryManager
        recovery_manager = RecoveryManager(
            self.db, self.settings.workspace_dir, self.settings.checkpoint_path
        )
        if registration.agent_type == "research" and self.quality_review_enabled:
            from app.runtime.lifecycle_coordinator import ResearchLifecycleCoordinator
            lifecycle_coordinator = ResearchLifecycleCoordinator(
                self.settings, self.db, task_id, artifacts, reviewer=self.research_reviewer
            )
        callback = TaskEventCallback(
            self.db, task_id, started, int(task["max_runtime_seconds"]),
            worker_id, task.get("worker_generation"), plan_progress_service,
            recovery_manager, lifecycle_coordinator,
        )
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
            human_decisions = (
                self.db.list_unconsumed_human_decisions(task_id, limit=10)
                if task.get("resume_mode") else []
            )
            human_messages = []
            for decision in human_decisions:
                event_payload = decision.get("payload") or {}
                decision_payload = event_payload.get("payload") or {}
                intervention_id = str(event_payload.get("intervention_id") or "")
                action = str(event_payload.get("action") or "")
                request_payload = event_payload.get("request") or {}
                instruction = {
                    "action": action,
                    "reason": decision_payload.get("reason", ""),
                    "direction": decision_payload.get("direction", ""),
                    "requirements": decision_payload.get("requirements"),
                    "request": request_payload,
                }
                content = (
                    "任务刚刚经过人工干预并已恢复。请严格执行以下人工决策，先核对研究状态和现有证据，"
                    "必要时更新计划，不要重复已经完成的研究步骤。\n"
                    + json.dumps(instruction, ensure_ascii=False, indent=2)
                    + "\n如需读取完整审计记录，请调用 get_human_decisions。"
                )
                from langchain_core.messages import HumanMessage
                human_messages.append(HumanMessage(
                    id=f"human-intervention-{intervention_id}", content=content,
                ))

            existing_message_ids = set()
            if snapshot is not None:
                values = getattr(snapshot, "values", None) or {}
                for message in values.get("messages", []) if isinstance(values, dict) else []:
                    message_id = message.get("id") if isinstance(message, dict) else getattr(message, "id", None)
                    if message_id:
                        existing_message_ids.add(str(message_id))
            unseen_human_messages = [
                message for message in human_messages
                if str(getattr(message, "id", "")) not in existing_message_ids
            ]

            if snapshot is not None and getattr(snapshot, "next", ()):
                self.db.add_event(task_id, "checkpoint_resume", {"pending_nodes": list(snapshot.next)})
                if unseen_human_messages:
                    updater = getattr(agent, "update_state", None)
                    if callable(updater):
                        updater(config, {"messages": unseen_human_messages})
                        result = agent.invoke(None, config=config)
                    else:
                        result = agent.invoke({"messages": unseen_human_messages}, config=config)
                else:
                    result = agent.invoke(None, config=config)
            elif snapshot is not None and getattr(snapshot, "values", None):
                self.db.add_event(task_id, "checkpoint_already_complete", {})
                result = snapshot.values
                resume_messages = list(unseen_human_messages)
                if task.get("resume_mode"):
                    events = self.db.get_events(task_id, limit=5000)
                    failed_validation = next((event for event in reversed(events) if event["event_type"] == "delivery_validation_completed" and not event["payload"].get("valid")), None)
                    if failed_validation:
                        repair = "上次交付校验未通过，请基于已保存的报告和以下校验反馈进行定向修复，不要重新执行已完成的研究：\\n" + str(failed_validation["payload"].get("errors", "")) + "\\n修复后覆盖岗位声明的产物文件。"
                        self.db.add_event(task_id, "delivery_repair_resumed", {"source_event_id": failed_validation["event_id"]})
                        resume_messages.append({"role": "user", "content": repair})
                if resume_messages:
                    result = agent.invoke({"messages": resume_messages}, config=config)
            else:
                self.db.add_event(task_id, "agent_initial_run", {})
                initial_messages = [{"role": "user", "content": task["goal"]}, *unseen_human_messages]
                result = agent.invoke({"messages": initial_messages}, config=config)

            for decision in human_decisions:
                intervention_id = str((decision.get("payload") or {}).get("intervention_id") or "")
                self.db.mark_human_decision_consumed(task_id, intervention_id)

            # Re-check after graph execution so a provider call that ran past the
            # deadline cannot be accepted as a successful final response.
            callback._check_control()

            existing_report = artifacts.read_text(artifact_path) if artifacts.exists(artifact_path) else ""
            if not existing_report.strip():
                final_text = extract_final_text(result)
                if not final_text.strip():
                    self.db.add_event(task_id, "model_empty_final_response_recovery_started", {
                        "artifact_path": artifact_path,
                        "reason": "Agent completed without a persisted artifact or final assistant text",
                    })
                    retry_result = agent.invoke(
                        {"messages": [{
                            "role": "user",
                            "content": (
                                "Your previous assistant turn contained no final text and no report artifact was saved. "
                                "Recover from the current checkpoint and saved evidence. Do not repeat completed research. "
                                "Write the complete deliverable to the required artifact path using save_artifact, "
                                "then provide a concise completion message."
                            ),
                        }]},
                        config=config,
                    )
                    result = retry_result
                    callback._check_control()
                    final_text = extract_final_text(result)
                if not final_text.strip():
                    raise DeliveryValidationExhausted(
                        f"Agent returned an empty final response and did not persist {artifact_path} after one recovery attempt."
                    )
                self._persist_artifact(task_id, artifacts, artifact_path, final_text, "final_assistant_response")
                self.db.add_event(task_id, "artifact_materialized_from_final_answer", {"path": artifact_path})
            else:
                self._verify_artifact(task_id, artifacts, artifact_path, "agent_saved_artifact")
                self.db.add_event(task_id, "artifact_saved_by_agent", {"path": artifact_path})

            if registration.validator is not None:
                valid = False
                for revision in range(registration.max_revisions + 1):
                    report = artifacts.read_text(artifact_path)
                    validation = registration.validator(report, task, artifacts)
                    structural_valid = self._validation_ok(validation)
                    quality_assessment = None
                    if lifecycle_coordinator is not None:
                        quality_assessment = lifecycle_coordinator.evaluate_delivery(
                            task, report, validation=validation, callback=callback, allow_llm=True
                        )
                        reviewer_status = (quality_assessment.get("reviewer") or {}).get("status")
                        if reviewer_status == "unavailable":
                            self._park_for_human(task_id, worker_id, "independent_review_unavailable", {
                                "message": "独立研究质量评审不可用，不能自动通过交付。",
                                "artifact_path": artifact_path,
                                "assessment": quality_assessment,
                                "choices": ["continue_research", "continue_repair", "approve_degraded_delivery", "terminate"],
                            })
                        if not quality_assessment.get("passed"):
                            quality_messages = [
                                str(item.get("message") or item.get("code") or "质量硬门槛未通过")
                                for item in (quality_assessment.get("hard_blocks") or [])
                            ] + [
                                str(item.get("message") or item.get("code") or "研究质量缺陷")
                                for item in (quality_assessment.get("defects") or [])
                            ]
                            if not quality_messages:
                                quality_messages.append(
                                    "五维质量评分未达到门槛："
                                    + str(quality_assessment.get("total_score"))
                                    + "；阈值：" + str(quality_assessment.get("thresholds"))
                                )
                            original_errors = getattr(validation, "errors", ()) or ()
                            validation = SimpleNamespace(
                                valid=False,
                                errors=tuple([*(original_errors if not isinstance(original_errors, str) else [original_errors]),
                                              *quality_messages]),
                                original_validation=validation,
                                quality_assessment=quality_assessment,
                            )
                    valid = structural_valid and (
                        quality_assessment is None or bool(quality_assessment.get("passed"))
                    )
                    errors = self._validation_errors(validation)
                    validation_payload = {
                        "agent_type": registration.agent_type,
                        "valid": valid,
                        "errors": errors[:4000],
                        "revision": revision,
                        "artifact_path": artifact_path,
                        "evidence_coverage": getattr(validation, "evidence_coverage", None),
                        "source_count": getattr(validation, "source_count", None),
                        "registered_source_count": getattr(validation, "registered_source_count", None),
                        "verified_source_count": getattr(validation, "verified_source_count", None),
                        "report_url_count": getattr(validation, "report_url_count", None),
                        "cited_source_count": getattr(validation, "cited_source_count", None),
                        "finding_count": getattr(validation, "finding_count", None),
                        "cited_source_ids": list(getattr(validation, "cited_source_ids", ()) or ()),
                        "cited_finding_ids": list(getattr(validation, "cited_finding_ids", ()) or ()),
                        "quality_assessment_id": (quality_assessment or {}).get("assessment_id"),
                        "quality_total_score": (quality_assessment or {}).get("total_score"),
                        "quality_dimension_scores": (quality_assessment or {}).get("dimension_scores"),
                        "quality_passed": (quality_assessment or {}).get("passed"),
                        "quality_reviewer_status": ((quality_assessment or {}).get("reviewer") or {}).get("status"),
                        "quality_hard_blocks": (quality_assessment or {}).get("hard_blocks", []),
                        "quality_defects": (quality_assessment or {}).get("defects", []),
                    }
                    self.db.add_event(task_id, "delivery_validation_completed", validation_payload)
                    callback.sync_plan_progress_for_phase(
                        "validation", "系统已开始校验最终报告及证据引用。"
                    )
                    if valid:
                        persisted = self._verify_artifact(task_id, artifacts, artifact_path, "validated_report")
                        digest = hashlib.sha256(persisted.encode("utf-8")).hexdigest()
                        self.db.add_event(task_id, "report_delivered", {
                            "artifact_path": artifact_path,
                            "bytes": len(persisted.encode("utf-8")),
                            "sha256": digest,
                            "validation_revision": revision,
                            "evidence_coverage": getattr(validation, "evidence_coverage", None),
                            "source_count": getattr(validation, "source_count", None),
                            "report_url_count": getattr(validation, "report_url_count", None),
                            "quality_assessment_id": (quality_assessment or {}).get("assessment_id"),
                            "quality_total_score": (quality_assessment or {}).get("total_score"),
                            "quality_dimension_scores": (quality_assessment or {}).get("dimension_scores"),
                        })
                        callback.complete_active_plan_step(
                            "最终报告已通过交付校验并正式交付。",
                            output_path=artifact_path,
                        )
                        break

                    if revision >= registration.max_revisions:
                        self.db.add_event(task_id, "delivery_validation_exhausted", {
                            "artifact_path": artifact_path,
                            "revisions": registration.max_revisions,
                            "errors": errors[:4000],
                            "artifact_preserved": artifacts.exists(artifact_path),
                            "quality_assessment_id": (quality_assessment or {}).get("assessment_id"),
                        })
                        if quality_assessment is not None and not quality_assessment.get("hard_blocks"):
                            self._park_for_human(task_id, worker_id, "quality_not_satisfied", {
                                "message": "报告已完成有限修复，但仍未达到岗位质量门槛。",
                                "artifact_path": artifact_path,
                                "assessment": quality_assessment,
                                "choices": ["continue_repair", "approve_degraded_delivery", "terminate"],
                            })
                        raise DeliveryValidationExhausted(
                            f"delivery validation failed after {registration.max_revisions} revisions: {errors}"
                        )
                    if not registration.postprocessor:
                        raise DeliveryValidationExhausted(
                            f"delivery validation failed and role has no repair handler: {errors}"
                        )

                    before_digest = hashlib.sha256(report.encode("utf-8")).hexdigest()
                    repaired_result = registration.postprocessor(
                        agent, result, config, task, artifacts, validation
                    )
                    if repaired_result is not None:
                        result = repaired_result
                    callback._check_control()

                    updated_report = artifacts.read_text(artifact_path) if artifacts.exists(artifact_path) else ""
                    after_digest = hashlib.sha256(updated_report.encode("utf-8")).hexdigest()
                    if not updated_report.strip() or after_digest == before_digest:
                        candidate = extract_final_text(result)
                        if candidate.strip() and candidate.strip() != report.strip():
                            self._persist_artifact(
                                task_id, artifacts, artifact_path, candidate,
                                f"repair_response_{revision + 1}",
                            )
                        else:
                            self.db.add_event(task_id, "delivery_repair_no_change", {
                                "artifact_path": artifact_path,
                                "revision": revision + 1,
                                "reason": "repair turn did not change the persisted report",
                            })
                if not valid:
                    raise DeliveryValidationExhausted("delivery validation did not pass")
            else:
                persisted = self._verify_artifact(task_id, artifacts, artifact_path, "artifact_delivery")
                self.db.add_event(task_id, "artifact_delivered", {
                    "artifact_path": artifact_path,
                    "bytes": len(persisted.encode("utf-8")),
                    "sha256": hashlib.sha256(persisted.encode("utf-8")).hexdigest(),
                    "validation_required": False,
                })
                callback.complete_active_plan_step(
                    "最终成果已持久化并完成交付。",
                    output_path=artifact_path,
                )
        return (artifacts.workspace / artifact_path).resolve().as_posix()
