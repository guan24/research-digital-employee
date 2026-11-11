"""Small dependency-free model-call budget estimation helpers."""

import json
import math

# ChatOpenAI receives the same hard output cap that the reservation includes.
MODEL_MAX_OUTPUT_TOKENS = 2048
MODEL_CALL_FIXED_OVERHEAD_TOKENS = 128


def estimate_model_call_reservation(request: object) -> int:
    """Conservatively reserve one token per serialized input character plus output and overhead.

    This deliberately overestimates typical English text. It avoids an external tokenizer
    dependency and gives a deterministic pre-provider gate for the text-only research Agent.
    """
    serialized = json.dumps(request, ensure_ascii=False, default=str, separators=(",", ":"))
    estimated_input = max(1, math.ceil(len(serialized)))
    return estimated_input + MODEL_MAX_OUTPUT_TOKENS + MODEL_CALL_FIXED_OVERHEAD_TOKENS
