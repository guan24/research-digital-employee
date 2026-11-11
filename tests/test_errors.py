from app.runtime.errors import classify_exception, DeliveryValidationExhausted


def test_authentication_is_terminal_and_unknown_is_retryable_unknown():
    class AuthenticationError(RuntimeError):
        pass
    category, code, retryable = classify_exception(AuthenticationError("invalid API key"))
    assert (category, code, retryable) == ("terminal", "PROVIDER_AUTH_OR_PERMISSION", False)
    category, code, retryable = classify_exception(RuntimeError("unclassified provider glitch"))
    assert (category, code, retryable) == ("unknown", "UNKNOWN_EXCEPTION", True)


def test_delivery_validation_exhaustion_is_terminal():
    category, code, retryable = classify_exception(DeliveryValidationExhausted("no citations"))
    assert (category, code, retryable) == ("terminal", "DELIVERY_VALIDATION_EXHAUSTED", False)
