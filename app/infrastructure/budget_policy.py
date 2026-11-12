from __future__ import annotations

CONVERGENCE_RESERVE_RATIO = 0.30
TOOL_CALL_CONVERGENCE_RESERVE_RATIO = 0.25
RUNTIME_CONVERGENCE_RESERVE_RATIO = 0.20
MIN_CONVERGENCE_RESERVE_TOKENS = 4_096
MIN_CONVERGENCE_TOOL_CALLS = 4
MIN_CONVERGENCE_RUNTIME_SECONDS = 60


def _reserve(limit: int, ratio: float, minimum: int) -> int:
    total = max(0, int(limit or 0))
    if total <= 0:
        return 0
    half = total // 2
    return min(max(int(total * ratio), min(int(minimum), half)), half)


def convergence_reserve_tokens(limit: int) -> int:
    """Reserve tokens for synthesis, report writing and quality repair."""
    return _reserve(limit, CONVERGENCE_RESERVE_RATIO, MIN_CONVERGENCE_RESERVE_TOKENS)


def convergence_reserve_tool_calls(limit: int) -> int:
    """Keep enough tool calls for evidence lookup, report save/read-back and repair."""
    return _reserve(limit, TOOL_CALL_CONVERGENCE_RESERVE_RATIO, MIN_CONVERGENCE_TOOL_CALLS)


def convergence_reserve_runtime_seconds(limit: int) -> int:
    """Keep execution time for synthesis, persistence, validation and repair."""
    return _reserve(limit, RUNTIME_CONVERGENCE_RESERVE_RATIO, MIN_CONVERGENCE_RUNTIME_SECONDS)
