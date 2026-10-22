"""The agent's own model spend is added up and held to its cap."""

from decimal import Decimal

import pytest
from acme_email.spend import SpendCappedProvider

from hecklr_llm import LLMError
from hecklr_schemas import LLMRequest, LLMResponse, Message

pytestmark = pytest.mark.anyio

REQUEST = LLMRequest(model="fake/brain", messages=[Message(role="user", content="hi")])


class PricedLLM:
    """Answers every request at a fixed price."""

    def __init__(self, cost_usd: str) -> None:
        self.cost_usd = Decimal(cost_usd)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(model=request.model, text="ok", cost_usd=self.cost_usd)


async def test_spend_adds_up_and_stops_once_past_the_cap() -> None:
    inner = PricedLLM("0.004")
    llm = SpendCappedProvider(inner, Decimal("0.010"))
    for _ in range(3):  # 0.004, 0.008, then 0.012: the call that crosses the cap was paid
        await llm.complete(REQUEST)
    assert llm.spent_usd == Decimal("0.012")
    with pytest.raises(LLMError, match=r"spent \$0.012 of its \$0.010 budget"):
        await llm.complete(REQUEST)
    assert inner.calls == 3


async def test_no_cap_only_counts() -> None:
    llm = SpendCappedProvider(PricedLLM("1"), None)
    for _ in range(3):
        await llm.complete(REQUEST)
    assert llm.spent_usd == 3
