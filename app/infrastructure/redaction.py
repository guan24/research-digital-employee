from __future__ import annotations

import re
from typing import Any

REDACTED = "[REDACTED]"
KEY_PATTERN = re.compile(
    r"(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|credential|connection[_-]?string|database[_-]?url|dsn)",
    re.IGNORECASE,
)
BEARER_PATTERN = re.compile(r"(?i)\b(Bearer\s+)[A-Za-z0-9._~+/-]+=*")
OPENAI_PATTERN = re.compile(r"\bsk-[A-Za-z0-9_-]{12,}\b")
TAVILY_PATTERN = re.compile(r"\btvly-[A-Za-z0-9_-]{8,}\b")
KEY_VALUE_PATTERN = re.compile(
    r"""(["']?(?:api[_-]?key|access[_-]?token|refresh[_-]?token|token|password|passwd|secret|authorization|client_secret)["']?\s*[:=]\s*["']?)([^"'\s,;&}\]]+)""",
    re.IGNORECASE,
)


def redact_text(value: str) -> str:
    value = KEY_VALUE_PATTERN.sub(lambda m: m.group(1) + REDACTED, value)
    value = BEARER_PATTERN.sub(lambda m: m.group(1) + REDACTED, value)
    value = OPENAI_PATTERN.sub(REDACTED, value)
    value = TAVILY_PATTERN.sub(REDACTED, value)
    return URL_CREDENTIALS.sub(lambda m: m.group(1) + REDACTED + "@", value)


def redact_value(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): (REDACTED if KEY_PATTERN.search(str(k)) else redact_value(v)) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [redact_value(v) for v in value]
    if isinstance(value, str):
        return redact_text(value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    return redact_text(str(value))
