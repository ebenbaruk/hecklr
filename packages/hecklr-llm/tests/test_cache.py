from collections.abc import Callable
from decimal import Decimal
from pathlib import Path

import anyio
import pytest

from hecklr_llm import CachedProvider, FakeLLM, LLMRequest, LLMResponse, request_key

RequestFactory = Callable[..., LLMRequest]


class CountingProvider:
    """Charges for every call and can be slowed down or made to fail."""

    def __init__(self, *, delay_s: float = 0.0, fail: bool = False) -> None:
        self.calls = 0
        self._delay_s = delay_s
        self._fail = fail
        self._fake = FakeLLM()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.calls += 1
        await anyio.sleep(self._delay_s)
        if self._fail:
            raise RuntimeError("provider down")
        reply = await self._fake.complete(request)
        return reply.model_copy(update={"cost_usd": Decimal("0.25")})


@pytest.mark.anyio
async def test_second_identical_call_is_free_and_marked_cached(
    tmp_path: Path, make_request: RequestFactory
) -> None:
    inner = CountingProvider()
    with CachedProvider(inner, tmp_path / "llm.sqlite3") as cache:
        first = await cache.complete(make_request("hi"))
        second = await cache.complete(make_request("hi"))

    assert inner.calls == 1
    assert (cache.misses, cache.hits) == (1, 1)
    assert not first.cached
    assert second.cached
    assert second.cost_usd == 0
    assert second.text == first.text


@pytest.mark.anyio
async def test_cache_survives_restarts(tmp_path: Path, make_request: RequestFactory) -> None:
    path = tmp_path / "nested" / "llm.sqlite3"
    with CachedProvider(CountingProvider(), path) as cache:
        await cache.complete(make_request("hi"))

    inner = CountingProvider()
    with CachedProvider(inner, path) as reopened:
        reply = await reopened.complete(make_request("hi"))

    assert inner.calls == 0
    assert reply.cached


@pytest.mark.anyio
@pytest.mark.parametrize(
    "change",
    [
        {"seed": 2},
        {"model": "fake/other"},
        {"system": "be terse"},
        {"temperature": 0.7},
        {"max_tokens": 10},
        {"json_schema": {"type": "object"}},
    ],
)
async def test_any_answer_changing_field_is_a_cache_miss(
    tmp_path: Path, make_request: RequestFactory, change: dict[str, object]
) -> None:
    inner = CountingProvider()
    with CachedProvider(inner, tmp_path / "llm.sqlite3") as cache:
        await cache.complete(make_request("hi"))
        await cache.complete(make_request("hi", **change))

    assert inner.calls == 2


def test_key_ignores_dict_ordering_inside_schemas(make_request: RequestFactory) -> None:
    a = make_request(json_schema={"type": "object", "properties": {"x": {"type": "string"}}})
    b = make_request(json_schema={"properties": {"x": {"type": "string"}}, "type": "object"})

    assert request_key(a) == request_key(b)
    assert len(request_key(a)) == 64


@pytest.mark.anyio
async def test_concurrent_identical_calls_pay_once(
    tmp_path: Path, make_request: RequestFactory
) -> None:
    inner = CountingProvider(delay_s=0.05)
    replies: list[LLMResponse] = []
    with CachedProvider(inner, tmp_path / "llm.sqlite3") as cache:

        async def call() -> None:
            replies.append(await cache.complete(make_request("same")))

        async with anyio.create_task_group() as group:
            for _ in range(8):
                group.start_soon(call)

        assert cache._key_locks == {}  # per-key locks are released

    assert inner.calls == 1
    assert sum(not reply.cached for reply in replies) == 1


@pytest.mark.anyio
async def test_different_requests_run_concurrently(
    tmp_path: Path, make_request: RequestFactory
) -> None:
    inner = CountingProvider(delay_s=0.2)
    with CachedProvider(inner, tmp_path / "llm.sqlite3") as cache:
        start = anyio.current_time()
        async with anyio.create_task_group() as group:
            for index in range(5):
                group.start_soon(cache.complete, make_request(f"q{index}"))
        elapsed = anyio.current_time() - start

    assert inner.calls == 5
    assert elapsed < 0.6


@pytest.mark.anyio
async def test_failures_are_not_cached(tmp_path: Path, make_request: RequestFactory) -> None:
    path = tmp_path / "llm.sqlite3"
    with CachedProvider(CountingProvider(fail=True), path) as cache:
        with pytest.raises(RuntimeError, match="provider down"):
            await cache.complete(make_request("hi"))
        assert cache._key_locks == {}

    healthy = CountingProvider()
    with CachedProvider(healthy, path) as cache:
        await cache.complete(make_request("hi"))
    assert healthy.calls == 1
