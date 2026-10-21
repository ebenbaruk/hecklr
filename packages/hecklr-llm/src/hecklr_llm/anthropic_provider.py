"""Anthropic Messages API provider."""

import anthropic
from anthropic.types import MessageParam, OutputConfigParam

from hecklr_llm.errors import LLMProviderError, LLMRefusalError, is_retryable_status
from hecklr_llm.model_names import split_model
from hecklr_llm.pricing import cost_usd
from hecklr_llm.structured import decode_json_reply
from hecklr_schemas import LLMRequest, LLMResponse, LLMUsage


class AnthropicProvider:
    """Completes requests for ``anthropic/<model>`` with the official async SDK.

    ``temperature`` and ``seed`` are not sent: current Claude models reject explicit sampling
    parameters and the API has no seed. Reproducibility comes from the response cache.
    """

    def __init__(
        self,
        client: anthropic.AsyncAnthropic | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        max_retries: int = 2,
        timeout_s: float = 120.0,
    ) -> None:
        self._client = client or anthropic.AsyncAnthropic(
            api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=timeout_s
        )

    async def aclose(self) -> None:
        """Close the SDK client and its connection pool."""
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one Messages API request and map the reply."""
        _, model = split_model(request.model)
        messages: list[MessageParam] = [
            {"role": message.role, "content": message.content} for message in request.messages
        ]
        output_config: OutputConfigParam | anthropic.Omit = anthropic.omit
        if request.json_schema is not None:
            schema: dict[str, object] = dict(request.json_schema)
            output_config = {"format": {"type": "json_schema", "schema": schema}}
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
        return LLMResponse(
            model=request.model,
            text=text,
            parsed=decode_json_reply(text) if request.json_schema is not None else None,
            usage=usage,
            cost_usd=cost_usd(request.model, usage),
        )
