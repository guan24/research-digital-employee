from __future__ import annotations

from app.agents.research.agent import resolve_summarization_token_limits


def test_qwen_3_8_flash_uses_documented_input_window():
    trigger_tokens, keep_tokens = resolve_summarization_token_limits("qwen3.8-flash")
    assert trigger_tokens == 773_610
    assert keep_tokens == 178_525


def test_explicit_model_input_limit_overrides_profile():
    trigger_tokens, keep_tokens = resolve_summarization_token_limits(
        "qwen3.8-flash",
        configured_max_input_tokens=100_000,
        model_profile={"max_input_tokens": 256_000},
    )
    assert (trigger_tokens, keep_tokens) == (78_000, 18_000)


def test_langchain_model_profile_is_used_when_no_override_is_configured():
    trigger_tokens, keep_tokens = resolve_summarization_token_limits(
        "some-profiled-model",
        model_profile={"max_input_tokens": 200_000},
    )
    assert (trigger_tokens, keep_tokens) == (156_000, 36_000)


def test_unknown_openai_compatible_model_uses_conservative_fallback():
    trigger_tokens, keep_tokens = resolve_summarization_token_limits("vendor/custom-model")
    assert (trigger_tokens, keep_tokens) == (99_840, 23_040)
