"""The model stack a run uses: response cache over per-provider rate limits over the router.

Order matters: the cache sits outside the rate limiter, so a cache hit is free and instant and
never waits for a provider slot.
"""

import time
from collections.abc import AsyncIterator, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from hecklr_llm import CachedProvider, ProviderRouter, split_model
from hecklr_schemas import HecklrConfig, LLMProvider, LLMRequest, LLMResponse

CACHE_FILE: Final = Path("cache/llm.sqlite3")
"""Response cache location, relative to the project's output directory (``.hecklr``)."""


@dataclass(frozen=True, slots=True)
class ProviderLimit:
    """How hard Hecklr may hit one provider prefix."""

    max_concurrent: int
    """Requests in flight at once."""
    requests_per_second: float | None = None
    """Sustained request rate (token bucket, burst of one second); ``None`` for no rate cap."""

    def __post_init__(self) -> None:
        if self.max_concurrent < 1:
            raise ValueError(f"max_concurrent must be >= 1, got {self.max_concurrent}")
        if self.requests_per_second is not None and self.requests_per_second <= 0:
            raise ValueError(f"requests_per_second must be > 0, got {self.requests_per_second}")


DEFAULT_PROVIDER_LIMITS: Final[Mapping[str, ProviderLimit]] = {
    # Hosted APIs: stay well inside the lowest paid tiers' concurrency.
    "anthropic": ProviderLimit(max_concurrent=16),
    "openai": ProviderLimit(max_concurrent=16),
    "openai-compatible": ProviderLimit(max_concurrent=16),
    # A local Ollama serves a handful of requests in parallel; more only queue inside it and
    # hit client timeouts.
    "ollama": ProviderLimit(max_concurrent=4),
}
"""Limits per provider prefix; prefixes not listed (``fake``) are not limited."""


class TokenBucket:
    """Allows ``rate`` acquisitions per second on average, with a burst of ``max(1, rate)``."""

    def __init__(self, rate: float) -> None:
        self._rate = rate
        self._capacity = max(1.0, rate)
        self._tokens = self._capacity
        self._updated = time.monotonic()
        self._lock = anyio.Lock()

    async def acquire(self) -> None:
        """Wait until a token is available and take it."""
        async with self._lock:
            now = time.monotonic()
            self._tokens = min(self._capacity, self._tokens + (now - self._updated) * self._rate)
            self._updated = now
            if self._tokens < 1:
                wait_s = (1 - self._tokens) / self._rate
                await anyio.sleep(wait_s)
                self._updated = time.monotonic()
                self._tokens = 0.0
            else:
                self._tokens -= 1


class RateLimitedProvider:
    """Caps concurrency and request rate per provider prefix (``anthropic/``, ``ollama/``...)."""

    def __init__(self, inner: LLMProvider, limits: Mapping[str, ProviderLimit]) -> None:
        self._inner = inner
        self._limiters = {
            prefix: anyio.CapacityLimiter(limit.max_concurrent) for prefix, limit in limits.items()
        }
        self._buckets = {
            prefix: TokenBucket(limit.requests_per_second)
            for prefix, limit in limits.items()
            if limit.requests_per_second is not None
        }

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Wait for the provider's slot and rate token, then complete ``request``."""
        prefix, _ = split_model(request.model)
        limiter = self._limiters.get(prefix)
        if limiter is None:
            return await self._inner.complete(request)
        async with limiter:
            bucket = self._buckets.get(prefix)
            if bucket is not None:
                await bucket.acquire()
            return await self._inner.complete(request)


@asynccontextmanager
async def open_llm(
    config: HecklrConfig,
    output_dir: Path,
    *,
    limits: Mapping[str, ProviderLimit] = DEFAULT_PROVIDER_LIMITS,
    providers: Mapping[str, LLMProvider] | None = None,
    env: Mapping[str, str] | None = None,
) -> AsyncIterator[CachedProvider]:
    """The run's model stack: ``CachedProvider(RateLimitedProvider(ProviderRouter))``.

    The cache lives at ``<output_dir>/cache/llm.sqlite3`` and is keyed by (model, prompt, seed),
    so re-running an unchanged trial costs zero tokens. ``providers`` pins provider instances
    per prefix (tests inject ``FakeLLM``); ``env`` is where API keys are read from.
    """
    router = ProviderRouter(
        config.models.providers, prices=config.models.prices, providers=providers, env=env
    )
    async with router:
        with CachedProvider(RateLimitedProvider(router, limits), output_dir / CACHE_FILE) as cache:
            yield cache
