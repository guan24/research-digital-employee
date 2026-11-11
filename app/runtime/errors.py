from __future__ import annotations

from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Any


@dataclass(frozen=True)
class ErrorClassification:
    category: str
    code: str
    retryable: bool
    summary: str
    details: dict[str, Any] | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


class DeliveryValidationError(RuntimeError):
    def __init__(self, message: str, feedback: str = ""):
        super().__init__(message)
        self.feedback = feedback


class MissingArtifactError(RuntimeError):
    """A role completed without its declared output artifact or final answer."""


def _status_code(exc: BaseException) -> int | None:
    value = getattr(exc, "status_code", None)
    if value is None:
        value = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def classify_exception(exc: BaseException) -> ErrorClassification:
    from app.runtime.control import (
        BudgetExceeded, TaskCancelled, TaskDeadlineExceeded, TaskPaused,
        TaskStepLimitExceeded, WorkerLeaseLost,
    )
    name = type(exc).__name__
    message = str(exc).strip() or name
    if isinstance(exc, DeliveryValidationError):
        return ErrorClassification("terminal", "DELIVERY_VALIDATION_FAILED", False, message, {"repair_feedback": exc.feedback})
    if isinstance(exc, MissingArtifactError):
        return ErrorClassification("terminal", "DELIVERABLE_MISSING", False, message)
    if isinstance(exc, TaskPaused):
        return ErrorClassification("control", "TASK_PAUSED", False, message)
    if isinstance(exc, TaskCancelled):
        return ErrorClassification("control", "TASK_CANCELLED", False, message)
    if isinstance(exc, WorkerLeaseLost):
        return ErrorClassification("control", "WORKER_LEASE_LOST", False, message)
    if isinstance(exc, BudgetExceeded):
        return ErrorClassification("control", exc.code, False, message, {"dimension": exc.dimension, **(exc.details or {})})
    if isinstance(exc, TaskStepLimitExceeded):
        return ErrorClassification("control", "TOOL_CALL_BUDGET_EXHAUSTED", False, message)
    if isinstance(exc, TaskDeadlineExceeded):
        return ErrorClassification("control", "RUNTIME_BUDGET_EXHAUSTED", False, message)

    status = _status_code(exc)
    lowered = f"{name} {message}".lower()
    if status == 429 or "ratelimit" in name.lower() or "rate limit" in lowered:
        return ErrorClassification("retryable", "UPSTREAM_RATE_LIMITED", True, message, {"status_code": status})
    if status is not None and status >= 500:
        return ErrorClassification("retryable", "UPSTREAM_5XX", True, message, {"status_code": status})
    if status in {408, 425} or any(token in name.lower() for token in ("timeout", "connection", "connecterror")):
        return ErrorClassification("retryable", "UPSTREAM_CONNECTION_FAILURE", True, message, {"status_code": status})
    if status == 401 or any(token in name.lower() for token in ("authenticationerror", "invalidapikey")):
        return ErrorClassification("terminal", "AUTHENTICATION_FAILED", False, message, {"status_code": status})
    if status == 403 or any(token in name.lower() for token in ("permissiondenied", "authorizationerror")):
        return ErrorClassification("terminal", "PERMISSION_DENIED", False, message, {"status_code": status})
    if status in {400, 404, 422} or any(token in name.lower() for token in ("badrequest", "validationerror", "invalidrequest")):
        code = "CONTEXT_TOO_LONG" if any(token in lowered for token in ("context length", "maximum context", "too many tokens", "context window")) else "INVALID_REQUEST"
        return ErrorClassification("terminal", code, False, message, {"status_code": status})
    if any(token in lowered for token in ("content_policy", "content policy", "safety system", "content filter")) or "contentpolicy" in name.lower():
        return ErrorClassification("terminal", "CONTENT_REVIEW_REJECTED", False, message)
    if isinstance(exc, ValueError):
        return ErrorClassification("terminal", "INVALID_ARGUMENT", False, message)
    if isinstance(exc, (TimeoutError, ConnectionError, OSError)):
        return ErrorClassification("retryable", "UPSTREAM_CONNECTION_FAILURE", True, message)
    if getattr(exc, "retryable", None) is True:
        return ErrorClassification("retryable", "UPSTREAM_TEMPORARY_UNAVAILABLE", True, message)
    if getattr(exc, "retryable", None) is False:
        return ErrorClassification("terminal", "UPSTREAM_REJECTED_REQUEST", False, message)
    return ErrorClassification("unknown", "UNKNOWN_ERROR", True, message, {"exception_type": name})


def error_record(classification: ErrorClassification, exc: BaseException, attempt: int) -> dict[str, Any]:
    return {
        "attempt": int(attempt),
        "timestamp": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "category": classification.category,
        "error_code": classification.code,
        "retryable": classification.retryable,
        "exception_type": type(exc).__name__,
        "summary": classification.summary[:4000],
        "details": classification.details or {},
    }
