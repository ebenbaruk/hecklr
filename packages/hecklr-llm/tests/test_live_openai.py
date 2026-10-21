"""Real calls to OpenAI's default live model (ADR 0007d). Opt in with ``pytest -m live``.

Paid: skipped unless ``OPENAI_API_KEY`` and ``HECKLR_TEST_BUDGET_USD`` are set. The whole file
costs well under a cent; each test asserts its own spend so a pricing slip shows up at once.
``HECKLR_LIVE_OPENAI_MODEL`` overrides the model.
"""

import os
from decimal import Decimal

import pytest
from pydantic import BaseModel

from hecklr_llm import LLMRequest, Message, ProviderRouter, complete_structured

MODEL = os.environ.get("HECKLR_LIVE_OPENAI_MODEL", "openai/gpt-6-luna")
MAX_COST_PER_TEST = Decimal("0.002")

pytestmark = [pytest.mark.live, pytest.mark.anyio]


@pytest.fixture(scope="module", autouse=True)
def _require_key_and_budget() -> None:
    if not os.environ.get("OPENAI_API_KEY"):
        pytest.skip("OPENAI_API_KEY is not set")
    if Decimal(os.environ.get("HECKLR_TEST_BUDGET_USD", "0")) < MAX_COST_PER_TEST * 3:
        pytest.skip("HECKLR_TEST_BUDGET_USD does not cover the OpenAI live tests")


class Booking(BaseModel):
    weekday: str
    morning: bool


def _booking_request(temperature: float | None) -> LLMRequest:
    return LLMRequest(
        model=MODEL,
        messages=[
            Message(
                role="user",
                content="The customer wants Tuesday at 9am. Extract weekday and morning.",
            )
        ],
        temperature=temperature,
        seed=1,
        max_tokens=1000,
    )


async def test_ws7d_live_luna_plain_completion_at_temperature_zero() -> None:
    async with ProviderRouter() as router:
        reply = await router.complete(
            LLMRequest(
                model=MODEL,
                system="Answer with one lowercase word and nothing else.",
                messages=[Message(role="user", content="What is the opposite of 'ping'?")],
                temperature=0.0,
                seed=1,
                max_tokens=20,
            )
        )

    assert "pong" in reply.text.lower()
    assert reply.usage.output_tokens > 0
    assert Decimal(0) < reply.cost_usd < MAX_COST_PER_TEST


@pytest.mark.parametrize("temperature", [0.0, None], ids=["reasoning-off", "reasoning-default"])
async def test_ws7d_live_luna_structured_completion(temperature: float | None) -> None:
    async with ProviderRouter() as router:
        booking, reply = await complete_structured(router, _booking_request(temperature), Booking)

    assert booking.weekday.lower().startswith("tue")
    assert booking.morning is True
    assert Decimal(0) < reply.cost_usd < MAX_COST_PER_TEST
