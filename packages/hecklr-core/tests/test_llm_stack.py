"""Event sink, cost metering with the hard budget, per-provider limits and the response cache."""

import time
from decimal import Decimal
from pathlib import Path

import anyio
import pytest

from hecklr_core import (
    BudgetExceededError,
    MeteredProvider,
    Project,
    ProviderLimit,
    RateLimitedProvider,
    RunBudget,
    TokenBucket,
    TrialEventSink,
    TrialSpend,
    open_llm,
)
from hecklr_core.llm import CACHE_FILE
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    FakeClock,
    LLMRequest,
    LLMResponse,
    Message,
    TrialId,
    UtterancePayload,
    new_id,
)


def _request(model: str = "fake/sim", text: str = "hi") -> LLMRequest:
    return LLMRequest(model=model, messages=[Message(role="user", content=text)], seed=1)


class _Costly:
    def __init__(self, cost: str) -> None:
        self.cost = Decimal(cost)
        self.calls = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        return LLMResponse(model=request.model, text="ok", cost_usd=self.cost)


# --- sink ---------------------------------------------------------------------------------------


def test_sink_stamps_ids_seq_and_clock() -> None:
    clock = FakeClock()
    trial_id = TrialId(new_id())
    sink = TrialEventSink(trial_id, clock)
    first = sink.emit("user", UtterancePayload(text="hello", turn=0))
    clock.advance(250)
    second = sink.emit("agent", UtterancePayload(text="hi", turn=1), t_ms=100)
    third = sink.emit("agent", UtterancePayload(text="bye", turn=2))
    assert [e.seq for e in sink.events] == [0, 1, 2]
    assert [e.t_ms for e in sink.events] == [0, 100, 250]
    assert {e.trial_id for e in sink.events} == {trial_id}
    assert len({first.id, second.id, third.id}) == 3
    assert sink.now_ms() == 250


# --- budget and metering ------------------------------------------------------------------------


def test_budget_is_exceeded_strictly_above_the_limit() -> None:
    at_limit = RunBudget(Decimal("1.00"))
    at_limit.charge(Decimal("1.00"))
    assert not at_limit.exceeded
    over = RunBudget(Decimal("1.00"), spent_usd=Decimal("1.00"))
    with pytest.raises(BudgetExceededError, match=r"spent \$1.01 of its \$1.00 budget"):
        over.charge(Decimal("0.01"))
    assert over.exceeded
    unlimited = RunBudget(None)
    unlimited.charge(Decimal(10**6))
    assert not unlimited.exceeded


@pytest.mark.anyio
async def test_metered_provider_attributes_cost_to_trial_and_run() -> None:
    budget = RunBudget(Decimal(1))
    first, second = TrialSpend(budget), TrialSpend(budget)
    inner = _Costly("0.25")
    await MeteredProvider(inner, first).complete(_request())
    await MeteredProvider(inner, first).complete(_request())
    await MeteredProvider(inner, second).complete(_request())
    assert first.total_usd == Decimal("0.50")
    assert second.total_usd == Decimal("0.25")
    assert budget.spent_usd == Decimal("0.75")


@pytest.mark.anyio
async def test_metered_provider_stops_at_the_budget() -> None:
    budget = RunBudget(Decimal("0.30"))
    spend = TrialSpend(budget)
    inner = _Costly("0.25")
    provider = MeteredProvider(inner, spend)
    await provider.complete(_request())
    with pytest.raises(BudgetExceededError):
        await provider.complete(_request())
    # The crossing call was paid for and is counted; nothing is sent afterwards.
    assert spend.total_usd == Decimal("0.50")
    with pytest.raises(BudgetExceededError):
        await provider.complete(_request())
    assert inner.calls == 2


# --- rate limits --------------------------------------------------------------------------------


class _Tracking:
    def __init__(self) -> None:
        self.active = 0
        self.peak = 0

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.active += 1
        self.peak = max(self.peak, self.active)
        await anyio.sleep(0.01)
        self.active -= 1
        return LLMResponse(model=request.model, text="ok")


@pytest.mark.anyio
async def test_rate_limiter_caps_concurrency_per_provider_prefix() -> None:
    inner = _Tracking()
    provider = RateLimitedProvider(inner, {"anthropic": ProviderLimit(max_concurrent=2)})
    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(provider.complete, _request("anthropic/claude-sonnet-5-5"))
    assert inner.peak == 2

    unlimited = _Tracking()
    provider = RateLimitedProvider(unlimited, {"anthropic": ProviderLimit(max_concurrent=2)})
    async with anyio.create_task_group() as group:
        for _ in range(8):
            group.start_soon(provider.complete, _request("fake/sim"))
    assert unlimited.peak == 8


@pytest.mark.anyio
async def test_token_bucket_spaces_requests() -> None:
    bucket = TokenBucket(rate=50.0)
    start = time.monotonic()
    for _ in range(60):
        await bucket.acquire()
    elapsed = time.monotonic() - start
    # A burst of 50, then 10 more at 50/s: about 0.2 s.
    assert 0.15 <= elapsed < 1.0


@pytest.mark.anyio
async def test_rate_limiter_applies_requests_per_second() -> None:
    provider = RateLimitedProvider(
        FakeLLM(), {"fake": ProviderLimit(max_concurrent=4, requests_per_second=20.0)}
    )
    start = time.monotonic()
    for index in range(25):
        await provider.complete(_request(text=str(index)))
    assert time.monotonic() - start >= 0.2


def test_provider_limit_validates() -> None:
    with pytest.raises(ValueError, match="max_concurrent"):
        ProviderLimit(max_concurrent=0)
    with pytest.raises(ValueError, match="requests_per_second"):
        ProviderLimit(max_concurrent=1, requests_per_second=0)


# --- response cache wiring ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_open_llm_caches_responses_under_the_output_dir(
    project: Project, tmp_path: Path
) -> None:
    fake = FakeLLM()
    output = tmp_path / ".hecklr"
    async with open_llm(project.config, output, providers={"fake": fake}) as llm:
        first = await llm.complete(_request())
    async with open_llm(project.config, output, providers={"fake": fake}) as llm:
        second = await llm.complete(_request())
        assert (llm.hits, llm.misses) == (1, 0)
    assert (output / CACHE_FILE).is_file()
    assert len(fake.calls) == 1
    assert second.cached
    assert second.cost_usd == 0
    assert second.text == first.text
