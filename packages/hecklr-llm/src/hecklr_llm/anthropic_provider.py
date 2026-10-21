"""Anthropic Messages API provider."""

from collections.abc import Mapping

import anthropic
from anthropic.types import MessageParam, OutputConfigParam

from hecklr_llm.errors import LLMProviderError, LLMRefusalError, is_retryable_status
from hecklr_llm.model_names import split_model
from hecklr_llm.pricing import PRICES, cost_usd, price_for
from hecklr_llm.structured import decode_json_reply
from hecklr_schemas import LLMRequest, LLMResponse, LLMUsage, ModelPrice


class AnthropicProvider:
    """Completes requests for ``anthropic/<model>`` with the official async SDK.

    ``temperature`` and ``seed`` are never sent, so on Anthropic they only shape the cache key.
    The API has no seed, and Opus 4.7+ and the 5.x models reject explicit sampling parameters;
    older models (Sonnet 4.6, Haiku 4.5) would accept a temperature, but one code path for every
    Claude model is simpler and reproducibility comes from the response cache anyway.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout_s: float = 120.0,
        prices: Mapping[str, ModelPrice] = PRICES,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=timeout_s
        )
        self._prices = prices

    async def aclose(self) -> None:
        """Close the SDK client and its connection pool."""
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one Messages API request and map the reply."""
        # Fail before spending money on a call whose cost could not be counted.
        price_for(request.model, self._prices)
        _, model = split_model(request.model)
        messages: list[MessageParam] = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        output_config: OutputConfigParam | anthropic.Omit = anthropic.omit
        if request.json_schema is not None:
            output_config = {
                "format": {"type": "json_schema", "schema": _strict_schema(request)},
            }
        try:
            reply = await self._client.messages.create(
                model=model,
                max_tokens=request.max_tokens,
                messages=messages,
                system=request.system if request.system is not None else anthropic.omit,
                output_config=output_config,
            )
        except anthropic.APIStatusError as exc:
            raise LLMProviderError(
                f"Anthropic API error {exc.status_code} for {request.model}: {exc.message}",
                retryable=is_retryable_status(exc.status_code),
                status_code=exc.status_code,
            ) from exc
        except anthropic.APIConnectionError as exc:
            raise LLMProviderError(
                f"cannot reach the Anthropic API for {request.model}: {exc}", retryable=True
            ) from exc
        except anthropic.AnthropicError as exc:
            raise LLMProviderError(
                f"Anthropic client error for {request.model}: {exc}", retryable=False
            ) from exc

        if reply.stop_reason == "refusal":
            raise LLMRefusalError(f"{request.model} declined the request")
        text = "".join(block.text for block in reply.content if block.type == "text")
        usage = LLMUsage(
            input_tokens=reply.usage.input_tokens, output_tokens=reply.usage.output_tokens
        )
        truncated = reply.stop_reason == "max_tokens"
        return LLMResponse(
            model=request.model,
            text=text,
            # A cut-off JSON reply cannot parse; complete_structured() reports it as truncated.
            parsed=(
                decode_json_reply(text)
                if request.json_schema is not None and not truncated
                else None
            ),
            usage=usage,
            cost_usd=cost_usd(request.model, usage, self._prices),
            truncated=truncated,
        )


def _strict_schema(request: LLMRequest) -> dict[str, object]:
    """Rewrite a JSON Schema into the subset Anthropic structured outputs accept.

    The API needs ``additionalProperties: false`` on every object and rejects keywords such as
    ``minimum`` or ``minLength`` that Pydantic emits; the SDK's ``transform_schema`` moves those
    into descriptions. The reply is still validated against the full schema client-side.
    """
    try:
        return anthropic.transform_schema(dict(request.json_schema or {}))
    except ValueError as exc:
        raise LLMProviderError(
            f"the response schema for {request.model} cannot be expressed as an Anthropic "
            f"structured output ({exc}); give every schema node a type",
            retryable=False,
        ) from exc
