from collections.abc import Callable

import pytest

from hecklr_llm import LLMRequest, Message

RequestFactory = Callable[..., LLMRequest]


def _make_request(
    text: str = "hello", *, model: str = "fake/sim", seed: int | None = 1, **fields: object
) -> LLMRequest:
    return LLMRequest.model_validate(
        {"model": model, "messages": [Message(role="user", content=text)], "seed": seed, **fields}
    )


@pytest.fixture
def make_request() -> RequestFactory:
    """Build an ``LLMRequest`` with one user message: ``make_request("hi", seed=3)``."""
    return _make_request
