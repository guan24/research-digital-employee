from __future__ import annotations

CONVERGENCE_RESERVE_RATIO = 0.30
MIN_CONVERGENCE_RESERVE_TOKENS = 4_096


def convergence_reserve_tokens(limit: int) -> int:
    """Reserve a protected share for synthesis, report writing and quality repair."""
    total = max(0, int(limit or 0))
    if total <= 0:
        return 0
    half = total // 2
    return min(
        max(int(total * CONVERGENCE_RESERVE_RATIO), min(MIN_CONVERGENCE_RESERVE_TOKENS, half)),
        half,
    )
