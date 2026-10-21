"""The LLM call contract shared by the simulator, judges, miner and fixer.

It lives here, not in ``hecklr-llm``, so that graders and other packages can accept an LLM handle
without depending on any provider SDK (ADR 0003). ``hecklr-llm`` implements it.
"""

from decimal import Decimal
from typing import Annotated, Literal, Protocol, runtime_checkable

from pydantic import Field, JsonValue

from hecklr_schemas.common import FrozenModel, ModelName


class Message(FrozenModel):
    """One chat message sent to a model."""

    role: Literal["user", "assistant"]
    content: str


class LLMRequest(FrozenModel):
    """A single, fully specified completion request.

    Everything that can change the answer is a field, so the request itself is the cache key.
    """

    model: ModelName
    messages: Annotated[list[Message], Field(min_length=1)]
    system: str | None = None
    temperature: Annotated[float, Field(ge=0.0, le=2.0)] | None = None
    """``None`` leaves the provider default; some models reject explicit sampling settings."""
    max_tokens: Annotated[int, Field(ge=1)] = 1024
    seed: int | None = None
    """Forwarded to providers that support it; always part of the cache key."""
    json_schema: dict[str, JsonValue] | None = None
    """When set, the model must answer with JSON matching this schema."""
    schema_name: Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")] = "response"


class LLMUsage(FrozenModel):
    """Token counts reported by the provider (estimated for FakeLLM)."""

    input_tokens: Annotated[int, Field(ge=0)] = 0
    output_tokens: Annotated[int, Field(ge=0)] = 0


class LLMResponse(FrozenModel):
    """A completion and what it cost."""

    model: ModelName
    text: str
    parsed: JsonValue = None
    """The decoded JSON when the request had a ``json_schema``."""
    usage: LLMUsage = LLMUsage()
    cost_usd: Annotated[Decimal, Field(ge=0)] = Decimal(0)
    """Zero when served from the response cache."""
    cached: bool = False


@runtime_checkable
class LLMProvider(Protocol):
    """Anything that can complete an ``LLMRequest``: a real provider, a router, a cache, FakeLLM."""

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Run one completion."""
        ...
