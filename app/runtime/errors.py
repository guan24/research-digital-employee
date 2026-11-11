from __future__ import annotations

from dataclasses import dataclass
from app.runtime.control import (
    TaskPaused, TaskCancelled, TaskDeadlineExceeded, TaskStepLimitExceeded,
    WorkerLeaseLost, TaskControlSignal,
)


class BudgetExhausted(TaskControlSignal):
    pass


@dataclass
class ClassifiedTaskError(RuntimeError):
    message: str
    category: str
    code: str
    retryable: bool

    def __str__(self) -> str:
        return self.message


class DeliveryValidationExhausted(ClassifiedTaskError):
    def __init__(self, message: str):
        super().__init__(message, "terminal", "DELIVERY_VALIDATION_EXHAUSTED", False)


def classify_exception(exc: BaseException) -> tuple[str, str, bool]:
    control = {
        TaskPaused: ("control", "TASK_PAUSED"),
        TaskCancelled: ("control", "TASK_CANCELLED"),
        TaskDeadlineExceeded: ("control", "RUNTIME_BUDGET_EXHAUSTED"),
        TaskStepLimitExceeded: ("control", "TOOL_STEP_BUDGET_EXHAUSTED"),
        WorkerLeaseLost: ("control", "WORKER_LEASE_LOST"),
        BudgetExhausted: ("control", "TOKEN_BUDGET_EXHAUSTED"),
    }
    for kind, result in control.items():
        if isinstance(exc, kind):
            return result[0], result[1], False
    if isinstance(exc, ClassifiedTaskError):
        return exc.category, exc.code, exc.retryable
    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if any(x in name for x in ("authentication", "permission")) or any(x in message for x in ("401", "403", "unauthorized", "forbidden", "invalid api key", "authentication failed")):
        return "terminal", "PROVIDER_AUTH_OR_PERMISSION", False
    if any(x in name for x in ("validation", "badrequest")):
        return "terminal", "PROVIDER_REQUEST_REJECTED", False
    if any(x in message for x in ("context length", "maximum context", "too many tokens")):
        return "terminal", "CONTEXT_TOO_LONG", False
    if any(x in message for x in ("content policy", "moderation", "safety system")):
        return "terminal", "CONTENT_REJECTED", False
    status = getattr(getattr(exc, "response", None), "status_code", None)
    if status == 429:
        return "retryable", "UPSTREAM_RATE_LIMIT", True
    if status and status >= 500:
        return "retryable", "UPSTREAM_5XX", True
    if status and 400 <= status < 500:
        return "terminal", "UPSTREAM_4XX", False
    if any(x in name for x in ("timeout", "connection", "connecterror")):
        return "retryable", "UPSTREAM_TRANSPORT", True
    return "unknown", "UNKNOWN_EXCEPTION", True
