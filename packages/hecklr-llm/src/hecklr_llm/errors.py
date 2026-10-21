"""Errors raised by the provider layer.

The orchestrator retries ``LLMProviderError`` with ``retryable=True`` (rate limits, server errors,
dropped connections) and treats everything else as a hard failure of the trial's harness.
"""


class LLMError(Exception):
    """Base class for every provider-layer error."""


class UnknownProviderError(LLMError):
    """A model string names a provider prefix the router does not know."""


class ProviderConfigError(LLMError):
    """A provider is missing configuration it needs, such as a base URL."""


class UnknownPriceError(LLMError):
    """A paid model has no known price, so its spend could not be held to the budget."""


class LLMProviderError(LLMError):
    """The provider API call failed after the SDK's own retries."""

    def __init__(self, message: str, *, retryable: bool, status_code: int | None = None) -> None:
        super().__init__(message)
        self.retryable = retryable
        self.status_code = status_code


class LLMRefusalError(LLMError):
    """The model declined to answer."""


class LLMOutputError(LLMError):
    """The model's reply does not match the requested structure."""


class FakeScriptExhaustedError(LLMError):
    """A scripted ``FakeLLM`` received more calls than it has replies."""


def is_retryable_status(status_code: int) -> bool:
    """Whether an HTTP status from a model API is worth retrying later."""
    return status_code in (408, 409, 429) or status_code >= 500
