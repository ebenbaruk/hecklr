from collections.abc import Callable
from decimal import Decimal

import pytest
from pydantic import BaseModel
from structlog.testing import capture_logs

from hecklr_llm import (
    FakeLLM,
    LLMOutputError,
    LLMRequest,
    LLMUsage,
    ModelPrice,
    complete_structured,
    cost_usd,
    estimate_input_tokens,
    estimate_request_cost,
    estimate_tokens,
    price_for,
)

RequestFactory = Callable[..., LLMRequest]


def test_known_model_cost_uses_per_million_prices() -> None:
    usage = LLMUsage(input_tokens=1_000_000, output_tokens=500_000)

    assert cost_usd("anthropic/claude-sonnet-5-5", usage) == Decimal("7.00")
    assert cost_usd("openai/gpt-4.1", usage) == Decimal("6.00")


@pytest.mark.parametrize("model", ["ollama/qwen3:8b", "fake/sim"])
def test_local_providers_are_free(model: str) -> None:
    assert cost_usd(model, LLMUsage(input_tokens=10**9, output_tokens=10**9)) == 0


def test_unknown_model_costs_zero_and_warns_once() -> None:
    with capture_logs() as logs:
        first = price_for("openai/gpt-unknown-test")
        price_for("openai/gpt-unknown-test")

    assert first == ModelPrice(Decimal(0), Decimal(0))
    assert [entry["event"] for entry in logs] == ["llm.price_unknown"]


def test_custom_price_table_is_honoured() -> None:
    prices = {"openai/custom": ModelPrice(Decimal(1), Decimal(2))}

    cost = cost_usd("openai/custom", LLMUsage(input_tokens=10**6, output_tokens=10**6), prices)

    assert cost == Decimal(3)


def test_token_estimates_are_deterministic() -> None:
    assert estimate_tokens("") == 0
    assert estimate_tokens("abcd") == 1
    assert estimate_tokens("abcde") == 2


def test_request_estimate_counts_system_messages_and_schema(make_request: RequestFactory) -> None:
    plain = make_request("x" * 400)
    rich = make_request("x" * 400, system="y" * 400, json_schema={"type": "object"})

    assert estimate_input_tokens(plain) == 100
    assert estimate_input_tokens(rich) > 200


def test_budget_estimate_assumes_max_tokens_unless_told(make_request: RequestFactory) -> None:
    request = make_request("x" * 4000, model="anthropic/claude-sonnet-5-5", max_tokens=1000)

    upper = estimate_request_cost(request)
    typical = estimate_request_cost(request, expected_output_tokens=100)

    assert upper == Decimal("0.012")  # 1000 in x $2/M + 1000 out x $10/M
    assert typical == Decimal("0.003")
    assert estimate_request_cost(make_request(model="ollama/qwen3:8b")) == 0


class Resolution(BaseModel):
    resolved: bool
    rationale: str


@pytest.mark.anyio
async def test_complete_structured_validates_into_the_model(make_request: RequestFactory) -> None:
    fake = FakeLLM(script=[Resolution(resolved=True, rationale="slot moved")])

    value, response = await complete_structured(fake, make_request("judge"), Resolution)

    assert value == Resolution(resolved=True, rationale="slot moved")
    sent = fake.calls[0]
    assert sent.json_schema == Resolution.model_json_schema()
    assert sent.schema_name == "Resolution"
    assert response.parsed == {"resolved": True, "rationale": "slot moved"}


@pytest.mark.anyio
async def test_complete_structured_rejects_mismatched_json(make_request: RequestFactory) -> None:
    fake = FakeLLM(script=['{"resolved": "maybe"}'])

    with pytest.raises(LLMOutputError, match="does not match Resolution"):
        await complete_structured(fake, make_request("judge"), Resolution)
