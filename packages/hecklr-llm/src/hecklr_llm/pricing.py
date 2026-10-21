"""Model prices for cost accounting and the orchestrator's pre-run budget check.

Prices fail closed: a paid model with no known price raises ``UnknownPriceError`` instead of
being counted as free, because a cost that reads zero would let a run silently overspend its
budget. Declare missing models (or a free self-hosted ``openai-compatible`` one, at 0) under
``models.prices`` in ``hecklr.yaml``.
"""

from collections.abc import Mapping
from decimal import Decimal
from typing import Final

from hecklr_llm.errors import UnknownPriceError
from hecklr_llm.model_names import split_model
from hecklr_llm.tokens import estimate_input_tokens
from hecklr_schemas import LLMRequest, LLMUsage, ModelPrice

_PER_TOKEN = Decimal(1) / Decimal(1_000_000)


def _price(input_per_mtok: str, output_per_mtok: str) -> ModelPrice:
    return ModelPrice(
        input_per_mtok=Decimal(input_per_mtok), output_per_mtok=Decimal(output_per_mtok)
    )


PRICES: Final[Mapping[str, ModelPrice]] = {
    # Anthropic first-party API list prices (2026-10), base tier. Haiku 5.5 bills prompts over
    # 100K input tokens at $0.50/$2.50; Hecklr's simulator and judge prompts stay far below that,
    # so the cheaper base tier is what applies in practice.
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
    # Hecklr's default live model (ADR 0007d) and its larger sibling.
    "openai/gpt-6-luna": _price("0.10", "0.50"),
    "openai/gpt-5.6-luna": _price("0.20", "1.20"),
}
"""Known prices keyed by full ``provider/model`` string."""

CACHED_INPUT_PER_MTOK: Final[Mapping[str, Decimal]] = {
    "openai/gpt-6-luna": Decimal("0.01"),
    "openai/gpt-5.6-luna": Decimal("0.02"),
}
"""USD per million prompt tokens the provider served from its prompt cache. ``ModelPrice`` has
no field for it (frozen schema), so it lives here; a model without an entry bills cached input at
its full input price, which can only overstate spend."""

FREE_PROVIDERS: Final = frozenset({"fake", "ollama"})
"""Providers that run locally and cost nothing."""

_FREE = ModelPrice(input_per_mtok=Decimal(0), output_per_mtok=Decimal(0))


def price_for(model: str, prices: Mapping[str, ModelPrice] = PRICES) -> ModelPrice:
    """Return the price of ``model``: zero for local providers, else from ``prices``.

    Raises:
        UnknownPriceError: ``model`` is served by a paid provider and has no entry in ``prices``.
    """
    provider, _ = split_model(model)
    if provider in FREE_PROVIDERS:
        return _FREE
    price = prices.get(model)
    if price is None:
        raise UnknownPriceError(
            f"no price is known for {model}, so its spend cannot be counted against the budget; "
            f"add it under models.prices in hecklr.yaml (input_per_mtok and output_per_mtok, "
            f"in USD; 0 for a free self-hosted model)"
        )
    return price


def cost_usd(
    model: str,
    usage: LLMUsage,
    prices: Mapping[str, ModelPrice] = PRICES,
    *,
    cached_input_tokens: int = 0,
) -> Decimal:
    """Cost of one completion with the given token usage.

    ``cached_input_tokens`` of ``usage.input_tokens`` bill at the model's cached-input rate
    (``CACHED_INPUT_PER_MTOK``), never above its input price.
    """
    price = price_for(model, prices)
    cached = min(cached_input_tokens, usage.input_tokens)
    cached_rate = min(CACHED_INPUT_PER_MTOK.get(model, price.input_per_mtok), price.input_per_mtok)
    return (
        (usage.input_tokens - cached) * price.input_per_mtok
        + cached * cached_rate
        + usage.output_tokens * price.output_per_mtok
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
