"""Model prices for cost accounting and the orchestrator's pre-run budget check."""

import functools
from collections.abc import Mapping
from dataclasses import dataclass
from decimal import Decimal
from typing import Final

import structlog

from hecklr_llm.model_names import split_model
from hecklr_llm.tokens import estimate_input_tokens
from hecklr_schemas import LLMRequest, LLMUsage

_log = structlog.get_logger(__name__)

_PER_TOKEN = Decimal(1) / Decimal(1_000_000)


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """List price in USD per million tokens."""

    input_per_mtok: Decimal
    output_per_mtok: Decimal


def _price(input_per_mtok: str, output_per_mtok: str) -> ModelPrice:
    return ModelPrice(Decimal(input_per_mtok), Decimal(output_per_mtok))


PRICES: Final[Mapping[str, ModelPrice]] = {
    # Anthropic first-party API list prices (2026-10).
    "anthropic/claude-opus-5-5": _price("4.00", "20.00"),
    "anthropic/claude-opus-5": _price("5.00", "25.00"),
    "anthropic/claude-opus-4-8": _price("5.00", "25.00"),
    "anthropic/claude-sonnet-5-5": _price("2.00", "10.00"),
    "anthropic/claude-sonnet-5": _price("2.00", "10.00"),
    "anthropic/claude-sonnet-4-6": _price("3.00", "15.00"),
    "anthropic/claude-haiku-5-5": _price("0.10", "0.50"),
    "anthropic/claude-haiku-4-5": _price("1.00", "5.00"),
    # OpenAI list prices.
    "openai/gpt-4.1": _price("2.00", "8.00"),
    "openai/gpt-4.1-mini": _price("0.40", "1.60"),
    "openai/gpt-4.1-nano": _price("0.10", "0.40"),
    "openai/gpt-4o": _price("2.50", "10.00"),
    "openai/gpt-4o-mini": _price("0.15", "0.60"),
}
"""Known prices keyed by full ``provider/model`` string."""

FREE_PROVIDERS: Final = frozenset({"fake", "ollama"})
"""Providers that run locally and cost nothing."""

_FREE = ModelPrice(Decimal(0), Decimal(0))


@functools.cache
def _warn_unknown_price(model: str) -> None:
    # Cached so a run with thousands of calls logs the warning once per model.
    _log.warning("llm.price_unknown", model=model, assumed_usd_per_mtok=0)


def price_for(model: str, prices: Mapping[str, ModelPrice] = PRICES) -> ModelPrice:
    """Return the price of ``model``; zero for local providers and (with a warning) unknown ones."""
    provider, _ = split_model(model)
    if provider in FREE_PROVIDERS:
        return _FREE
    price = prices.get(model)
    if price is None:
        _warn_unknown_price(model)
        return _FREE
    return price


def cost_usd(model: str, usage: LLMUsage, prices: Mapping[str, ModelPrice] = PRICES) -> Decimal:
    """Cost of one completion with the given token usage."""
    price = price_for(model, prices)
    return (
        usage.input_tokens * price.input_per_mtok + usage.output_tokens * price.output_per_mtok
    ) * _PER_TOKEN


def estimate_request_cost(
    request: LLMRequest,
    *,
    expected_output_tokens: int | None = None,
    prices: Mapping[str, ModelPrice] = PRICES,
) -> Decimal:
    """Upper-bound cost of ``request`` before it is sent, for budget planning.

    Output defaults to ``max_tokens`` so the estimate errs on the side of refusing a run.
    """
    output_tokens = request.max_tokens if expected_output_tokens is None else expected_output_tokens
    usage = LLMUsage(input_tokens=estimate_input_tokens(request), output_tokens=output_tokens)
    return cost_usd(request.model, usage, prices)
