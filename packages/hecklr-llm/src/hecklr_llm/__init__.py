"""LLM provider layer for Hecklr: Anthropic, OpenAI-compatible, FakeLLM and a response cache.

Typical wiring::

    llm = CachedProvider(ProviderRouter(config.models.providers))
    reply = await llm.complete(LLMRequest(model="anthropic/claude-sonnet-5-5", messages=[...]))
"""

from hecklr_llm.anthropic_provider import AnthropicProvider
from hecklr_llm.cache import DEFAULT_CACHE_PATH, CachedProvider
from hecklr_llm.errors import (
    FakeScriptExhaustedError,
    LLMError,
    LLMOutputError,
    LLMProviderError,
    LLMRefusalError,
    ProviderConfigError,
    UnknownProviderError,
)
from hecklr_llm.fake import FakeLLM, FakeReply, Responder, minimal_instance
from hecklr_llm.keys import request_key
from hecklr_llm.model_names import split_model
from hecklr_llm.openai_provider import OpenAIProvider
from hecklr_llm.pricing import PRICES, ModelPrice, cost_usd, estimate_request_cost, price_for
from hecklr_llm.router import KNOWN_PROVIDERS, ProviderRouter
from hecklr_llm.structured import complete_structured
from hecklr_llm.tokens import estimate_input_tokens, estimate_tokens
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse, LLMUsage, Message

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_CACHE_PATH",
    "KNOWN_PROVIDERS",
    "PRICES",
    "AnthropicProvider",
    "CachedProvider",
    "FakeLLM",
    "FakeReply",
    "FakeScriptExhaustedError",
    "LLMError",
    "LLMOutputError",
    "LLMProvider",
    "LLMProviderError",
    "LLMRefusalError",
    "LLMRequest",
    "LLMResponse",
    "LLMUsage",
    "Message",
    "ModelPrice",
    "OpenAIProvider",
    "ProviderConfigError",
    "ProviderRouter",
    "Responder",
    "UnknownProviderError",
    "complete_structured",
    "cost_usd",
    "estimate_input_tokens",
    "estimate_request_cost",
    "estimate_tokens",
    "minimal_instance",
    "price_for",
    "request_key",
    "split_model",
]
