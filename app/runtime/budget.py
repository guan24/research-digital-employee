"""Dependency-free token reservation helpers."""

import json
import math

MODEL_MAX_OUTPUT_TOKENS = 4_096
MODEL_CALL_FIXED_OVERHEAD_TOKENS = 256


def estimate_input_tokens(request: object) -> int:
    """Estimate multilingual input tokens without requiring an external tokenizer."""
    serialized = json.dumps(request, ensure_ascii=False, default=str, separators=(",", ":"))
    cjk = sum(
        1 for char in serialized
        if (
            0x3400 <= ord(char) <= 0x4DBF
            or 0x4E00 <= ord(char) <= 0x9FFF
            or 0xF900 <= ord(char) <= 0xFAFF
            or 0x3040 <= ord(char) <= 0x30FF
            or 0xAC00 <= ord(char) <= 0xD7AF
        )
    )
    other = max(0, len(serialized) - cjk)
    character_estimate = cjk * 1.2 + other / 3.2
    byte_estimate = len(serialized.encode("utf-8")) / 4
    return max(1, math.ceil(max(character_estimate, byte_estimate) * 1.12))


def estimate_model_call_reservation(
    request: object,
    max_output_tokens: int = MODEL_MAX_OUTPUT_TOKENS,
) -> int:
    """Reserve estimated input, the provider output cap and framing overhead."""
    return (
        estimate_input_tokens(request)
        + max(1, int(max_output_tokens))
        + MODEL_CALL_FIXED_OVERHEAD_TOKENS
    )
