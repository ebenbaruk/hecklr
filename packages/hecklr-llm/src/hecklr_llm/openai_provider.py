"""OpenAI Chat Completions provider, also used for any OpenAI-compatible endpoint."""

from typing import Literal

import openai
from openai.types.chat import ChatCompletionMessageParam
from openai.types.shared_params import ResponseFormatJSONSchema

from hecklr_llm.errors import LLMProviderError, LLMRefusalError, is_retryable_status
from hecklr_llm.model_names import split_model
from hecklr_llm.pricing import cost_usd
from hecklr_llm.structured import decode_json_reply
from hecklr_schemas import LLMRequest, LLMResponse, LLMUsage

OutputLimitParam = Literal["max_tokens", "max_completion_tokens"]


class OpenAIProvider:
    """Completes requests over ``/v1/chat/completions``.

    Serves ``openai/``, ``ollama/`` and ``openai-compatible/`` models; only the base URL, the key
    and the name of the output-limit parameter differ. OpenAI's own API wants
    ``max_completion_tokens``; most compatible servers (Ollama, vLLM) still read ``max_tokens``.
    """

    def __init__(
        self,
        client: openai.AsyncOpenAI | None = None,
        *,
        api_key: str | None = None,
        base_url: str | None = None,
        output_limit_param: OutputLimitParam = "max_completion_tokens",
        max_retries: int = 2,
        timeout_s: float = 120.0,
    ) -> None:
        self._client = client or openai.AsyncOpenAI(
            api_key=api_key, base_url=base_url, max_retries=max_retries, timeout=timeout_s
        )
        self._output_limit_param = output_limit_param

    async def aclose(self) -> None:
        """Close the SDK client and its connection pool."""
        await self._client.close()

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Send one chat completion request and map the reply."""
        _, model = split_model(request.model)
        messages: list[ChatCompletionMessageParam] = []
        if request.system is not None:
            messages.append({"role": "system", "content": request.system})
        for message in request.messages:
            if message.role == "user":
                messages.append({"role": "user", "content": message.content})
            else:
                messages.append({"role": "assistant", "content": message.content})
        response_format: ResponseFormatJSONSchema | openai.Omit = openai.omit
        if request.json_schema is not None:
            # Not "strict": strict mode rejects common schemas (optional fields, defaults);
            # replies are validated by complete_structured() instead.
            schema: dict[str, object] = dict(request.json_schema)
            response_format = {
                "type": "json_schema",
                "json_schema": {"name": request.schema_name, "schema": schema},
            }
        limit = request.max_tokens
        try:
            completion = await self._client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=request.temperature if request.temperature is not None else openai.omit,
                seed=request.seed if request.seed is not None else openai.omit,
                response_format=response_format,
                max_tokens=limit if self._output_limit_param == "max_tokens" else openai.omit,
                max_completion_tokens=(
                    limit if self._output_limit_param == "max_completion_tokens" else openai.omit
                ),
            )
        except openai.APIStatusError as exc:
            raise LLMProviderError(
                f"OpenAI-compatible API error {exc.status_code} for {request.model}: {exc.message}",
                retryable=is_retryable_status(exc.status_code),
                status_code=exc.status_code,
            ) from exc
        except openai.APIConnectionError as exc:
            raise LLMProviderError(
                f"cannot reach the endpoint for {request.model}: {exc}", retryable=True
            ) from exc
        except openai.OpenAIError as exc:
            raise LLMProviderError(
                f"OpenAI client error for {request.model}: {exc}", retryable=False
            ) from exc

        if not completion.choices:
            raise LLMProviderError(f"{request.model} returned no choices", retryable=True)
        reply = completion.choices[0].message
        if reply.refusal:
            raise LLMRefusalError(f"{request.model} declined the request: {reply.refusal}")
        text = reply.content or ""
        usage = LLMUsage(
            input_tokens=completion.usage.prompt_tokens if completion.usage else 0,
            output_tokens=completion.usage.completion_tokens if completion.usage else 0,
        )
        return LLMResponse(
            model=request.model,
            text=text,
            parsed=decode_json_reply(text) if request.json_schema is not None else None,
            usage=usage,
            cost_usd=cost_usd(request.model, usage),
        )
