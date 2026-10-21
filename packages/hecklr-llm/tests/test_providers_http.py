"""Provider request mapping through the real SDKs against a mocked HTTP transport (no network)."""

import json
from collections.abc import Callable
from decimal import Decimal
from typing import Any

import anthropic
import httpx2
import openai
import pytest

from hecklr_llm import (
    AnthropicProvider,
    LLMProviderError,
    LLMRefusalError,
    LLMRequest,
    Message,
    OpenAIProvider,
)

Handler = Callable[[httpx2.Request], httpx2.Response]


class Recorder:
    """Mock transport handler that records request bodies and replies with a canned response."""

    def __init__(self, status: int = 200, body: dict[str, Any] | None = None) -> None:
        self.status = status
        self.body = body or {}
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return httpx2.Response(self.status, json=self.body)

    @property
    def sent(self) -> dict[str, Any]:
        payload: dict[str, Any] = json.loads(self.requests[-1].content)
        return payload


def _anthropic(handler: Handler) -> AnthropicProvider:
    client = anthropic.AsyncAnthropic(
        api_key="test-key",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return AnthropicProvider(client)


def _openai(handler: Handler, **kwargs: Any) -> OpenAIProvider:
    client = openai.AsyncOpenAI(
        api_key="test-key",
        base_url="http://llm.test/v1",
        max_retries=0,
        http_client=httpx2.AsyncClient(transport=httpx2.MockTransport(handler)),
    )
    return OpenAIProvider(client, **kwargs)


def _request(model: str, **fields: Any) -> LLMRequest:
    return LLMRequest.model_validate(
        {
            "model": model,
            "messages": [
                Message(role="user", content="Move my visit"),
                Message(role="assistant", content="Which day?"),
                Message(role="user", content="Next Tuesday"),
            ],
            **fields,
        }
    )


def _anthropic_message(
    content: list[dict[str, Any]], stop_reason: str = "end_turn"
) -> dict[str, Any]:
    return {
        "id": "msg_1",
        "type": "message",
        "role": "assistant",
        "model": "claude-sonnet-5-5",
        "content": content,
        "stop_reason": stop_reason,
        "stop_sequence": None,
        "usage": {"input_tokens": 1200, "output_tokens": 300},
    }


def _chat_completion(content: str | None, refusal: str | None = None) -> dict[str, Any]:
    return {
        "id": "chatcmpl-1",
        "object": "chat.completion",
        "created": 0,
        "model": "gpt-4.1",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": content, "refusal": refusal},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 1000, "completion_tokens": 200, "total_tokens": 1200},
    }


# --- Anthropic ------------------------------------------------------------------------------


@pytest.mark.anyio
async def test_anthropic_maps_request_and_response() -> None:
    recorder = Recorder(
        body=_anthropic_message(
            [
                {"type": "thinking", "thinking": "", "signature": "sig"},
                {"type": "text", "text": "Tuesday "},
                {"type": "text", "text": "works."},
            ]
        )
    )

    reply = await _anthropic(recorder).complete(
        _request(
            "anthropic/claude-sonnet-5-5",
            system="You are a customer.",
            max_tokens=256,
            temperature=0.3,
            seed=9,
        )
    )

    sent = recorder.sent
    assert recorder.requests[-1].url.path == "/v1/messages"
    assert sent["model"] == "claude-sonnet-5-5"
    assert sent["system"] == "You are a customer."
    assert sent["max_tokens"] == 256
    assert [m["role"] for m in sent["messages"]] == ["user", "assistant", "user"]
    assert "temperature" not in sent
    assert "seed" not in sent
    assert "output_config" not in sent
    assert reply.text == "Tuesday works."
    assert reply.model == "anthropic/claude-sonnet-5-5"
    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (1200, 300)
    assert reply.cost_usd == Decimal("0.0054")
    assert reply.parsed is None


@pytest.mark.anyio
async def test_anthropic_structured_output_uses_output_config() -> None:
    schema = {
        "type": "object",
        "properties": {"resolved": {"type": "boolean"}},
        "required": ["resolved"],
        "additionalProperties": False,
    }
    recorder = Recorder(body=_anthropic_message([{"type": "text", "text": '{"resolved": true}'}]))

    reply = await _anthropic(recorder).complete(
        _request("anthropic/claude-haiku-5-5", json_schema=schema)
    )

    assert recorder.sent["output_config"] == {"format": {"type": "json_schema", "schema": schema}}
    assert "system" not in recorder.sent
    assert reply.parsed == {"resolved": True}


@pytest.mark.anyio
async def test_anthropic_refusal_raises() -> None:
    recorder = Recorder(body=_anthropic_message([], stop_reason="refusal"))

    with pytest.raises(LLMRefusalError, match="declined"):
        await _anthropic(recorder).complete(_request("anthropic/claude-opus-5-5"))


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "retryable"), [(429, True), (529, True), (400, False)])
async def test_anthropic_http_errors_are_classified(status: int, retryable: bool) -> None:
    recorder = Recorder(
        status=status,
        body={"type": "error", "error": {"type": "api_error", "message": "nope"}},
    )

    with pytest.raises(LLMProviderError) as caught:
        await _anthropic(recorder).complete(_request("anthropic/claude-sonnet-5-5"))

    assert caught.value.status_code == status
    assert caught.value.retryable is retryable


@pytest.mark.anyio
async def test_anthropic_connection_failure_is_retryable() -> None:
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LLMProviderError, match="cannot reach") as caught:
        await _anthropic(unreachable).complete(_request("anthropic/claude-sonnet-5-5"))

    assert caught.value.retryable


# --- OpenAI-compatible ----------------------------------------------------------------------


@pytest.mark.anyio
async def test_openai_maps_request_and_response() -> None:
    recorder = Recorder(body=_chat_completion("Tuesday works."))

    reply = await _openai(recorder).complete(
        _request(
            "openai/gpt-4.1", system="You are a judge.", temperature=0.0, seed=9, max_tokens=64
        )
    )

    sent = recorder.sent
    assert recorder.requests[-1].url.path == "/v1/chat/completions"
    assert sent["model"] == "gpt-4.1"
    assert sent["messages"][0] == {"role": "system", "content": "You are a judge."}
    assert [m["role"] for m in sent["messages"][1:]] == ["user", "assistant", "user"]
    assert sent["temperature"] == 0.0
    assert sent["seed"] == 9
    assert sent["max_completion_tokens"] == 64
    assert "max_tokens" not in sent
    assert "response_format" not in sent
    assert reply.text == "Tuesday works."
    assert reply.cost_usd == Decimal("0.0036")


@pytest.mark.anyio
async def test_openai_compatible_servers_get_max_tokens_and_json_schema() -> None:
    recorder = Recorder(body=_chat_completion('{"resolved": false}'))
    schema = {"type": "object", "properties": {"resolved": {"type": "boolean"}}}

    reply = await _openai(recorder, output_limit_param="max_tokens").complete(
        _request("ollama/qwen3:8b", json_schema=schema, schema_name="Verdict")
    )

    sent = recorder.sent
    assert sent["model"] == "qwen3:8b"
    assert sent["max_tokens"] == 1024
    assert "max_completion_tokens" not in sent
    assert "temperature" not in sent
    assert "seed" not in sent
    assert sent["response_format"] == {
        "type": "json_schema",
        "json_schema": {"name": "Verdict", "schema": schema},
    }
    assert reply.parsed == {"resolved": False}
    assert reply.cost_usd == 0


@pytest.mark.anyio
async def test_openai_refusal_raises() -> None:
    recorder = Recorder(body=_chat_completion(None, refusal="I can't help with that."))

    with pytest.raises(LLMRefusalError, match="can't help"):
        await _openai(recorder).complete(_request("openai/gpt-4.1"))


@pytest.mark.anyio
async def test_openai_without_choices_is_a_retryable_error() -> None:
    body = _chat_completion("x")
    body["choices"] = []

    with pytest.raises(LLMProviderError, match="no choices") as caught:
        await _openai(Recorder(body=body)).complete(_request("openai/gpt-4.1"))

    assert caught.value.retryable


@pytest.mark.anyio
async def test_openai_missing_usage_counts_as_zero() -> None:
    body = _chat_completion("ok")
    del body["usage"]

    reply = await _openai(Recorder(body=body)).complete(_request("openai/gpt-4.1"))

    assert (reply.usage.input_tokens, reply.usage.output_tokens) == (0, 0)


@pytest.mark.anyio
@pytest.mark.parametrize(("status", "retryable"), [(503, True), (401, False)])
async def test_openai_http_errors_are_classified(status: int, retryable: bool) -> None:
    recorder = Recorder(status=status, body={"error": {"message": "nope", "type": "x"}})

    with pytest.raises(LLMProviderError) as caught:
        await _openai(recorder).complete(_request("openai/gpt-4.1"))

    assert caught.value.retryable is retryable


@pytest.mark.anyio
async def test_openai_connection_failure_is_retryable() -> None:
    def unreachable(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(LLMProviderError, match="cannot reach") as caught:
        await _openai(unreachable).complete(_request("ollama/qwen3:8b"))

    assert caught.value.retryable


@pytest.mark.anyio
async def test_malformed_responses_are_non_retryable_client_errors() -> None:
    def garbage(request: httpx2.Request) -> httpx2.Response:
        return httpx2.Response(200, json={"unexpected": True})

    transport = httpx2.MockTransport(garbage)
    strict_anthropic = AnthropicProvider(
        anthropic.AsyncAnthropic(
            api_key="test-key",
            max_retries=0,
            _strict_response_validation=True,
            http_client=httpx2.AsyncClient(transport=transport),
        )
    )
    strict_openai = OpenAIProvider(
        openai.AsyncOpenAI(
            api_key="test-key",
            base_url="http://llm.test/v1",
            max_retries=0,
            _strict_response_validation=True,
            http_client=httpx2.AsyncClient(transport=transport),
        )
    )

    for provider, model in ((strict_anthropic, "anthropic/x"), (strict_openai, "openai/x")):
        with pytest.raises(LLMProviderError, match="client error") as caught:
            await provider.complete(_request(model))
        assert not caught.value.retryable
