"""Edge cases of the ``http`` and ``openai-compatible`` adapters over a mocked transport."""

import json
from collections.abc import Callable
from typing import Any

import httpx2
import pytest

from hecklr_adapters import (
    AdapterConfigError,
    AgentError,
    HttpChatTarget,
    OpenAICompatibleChatTarget,
)
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    ChatTarget,
    HandoffPayload,
    HttpAdapterConfig,
    OpenAICompatibleAdapterConfig,
    SessionContext,
    TrialId,
    new_id,
)

pytestmark = pytest.mark.anyio

Handler = Callable[[httpx2.Request], httpx2.Response]
CTX = SessionContext(trial_id=TrialId(new_id()), seed=1)


def _http(handler: Handler, **fields: Any) -> HttpChatTarget:
    config = HttpAdapterConfig.model_validate(
        {"type": "http", "url": "http://agent.test/chat", **fields}
    )
    return HttpChatTarget(config, transport=httpx2.MockTransport(handler))


def _openai(
    handler: Handler, environ: dict[str, str] | None = None, **fields: Any
) -> OpenAICompatibleChatTarget:
    config = OpenAICompatibleAdapterConfig.model_validate(
        {"type": "openai-compatible", "url": "http://agent.test/v1/", **fields}
    )
    return OpenAICompatibleChatTarget(
        config, transport=httpx2.MockTransport(handler), environ=environ or {}
    )


def _sse(body: str) -> httpx2.Response:
    return httpx2.Response(200, headers={"content-type": "text/event-stream"}, content=body)


async def _reply(target: ChatTarget, text: str = "hi") -> list[AgentChunk]:
    session = await target.open(CTX)
    chunks = [chunk async for chunk in session.send(text)]
    await session.close()
    return chunks


class Recorder:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response

    @property
    def body(self) -> Any:
        return json.loads(self.requests[-1].content)


# --- http --------------------------------------------------------------------------------------


async def test_http_without_response_path_uses_the_whole_body_and_default_template() -> None:
    recorder = Recorder(httpx2.Response(200, text="plain reply"))
    target = _http(recorder, headers={"Authorization": "Bearer x"})
    chunks = await _reply(target, 'say "hi"\n')
    assert [c.text for c in chunks] == ["plain reply", ""]
    assert recorder.body == {"message": 'say "hi"\n', "session_id": recorder.body["session_id"]}
    headers = recorder.requests[-1].headers
    assert headers["Authorization"] == "Bearer x"
    assert headers[TRIAL_ID_HEADER] == CTX.trial_id


async def test_http_keeps_one_session_id_for_the_whole_conversation() -> None:
    recorder = Recorder(httpx2.Response(200, text="ok"))
    session = await _http(recorder).open(CTX)
    for text in ("a", "b"):
        _ = [chunk async for chunk in session.send(text)]
    summary = await session.close()
    ids = {json.loads(request.content)["session_id"] for request in recorder.requests}
    assert ids == {summary.agent_session_id}


async def test_http_sse_named_events_become_tool_handoff_and_end_chunks() -> None:
    tool_call = {"call_id": "c1", "tool": "lookup", "args": {"id": "C-1"}}
    tool_result = {"call_id": "c1", "tool": "lookup", "result": {"ok": True}, "latency_ms": 12}
    body = (
        "data: Let me check\n\n"
        f"event: tool_call\ndata: {json.dumps(tool_call)}\n\n"
        f"event: tool_result\ndata: {json.dumps(tool_result)}\n\n"
        'event: handoff\ndata: {"reason": "gas smell"}\n\n'
        "event: end\ndata:\n\n"
    )
    chunks = await _reply(_http(lambda _: _sse(body), stream="sse"))
    assert chunks[0].text == "Let me check"
    assert chunks[1].tool_call is not None
    assert chunks[1].tool_call.args == {"id": "C-1"}
    assert chunks[2].tool_result is not None
    assert chunks[2].tool_result.latency_ms == 12
    assert chunks[3].handoff == HandoffPayload(reason="gas smell")
    assert chunks[4].ended
    assert chunks[-1].final


async def test_http_sse_response_path_reads_json_deltas() -> None:
    body = 'data: {"t": "a"}\n\ndata: {"t": "b"}\n\n'
    chunks = await _reply(_http(lambda _: _sse(body), stream="sse", response_path="t"))
    assert "".join(c.text for c in chunks) == "ab"


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ("event: mystery\ndata: x\n\n", "unknown server-sent event"),
        ("event: tool_call\ndata: {}\n\n", "malformed 'tool_call'"),
    ],
)
async def test_http_sse_rejects_unknown_or_malformed_events(body: str, match: str) -> None:
    with pytest.raises(AgentError, match=match):
        await _reply(_http(lambda _: _sse(body), stream="sse"))


async def test_http_sse_with_wrong_content_type_is_an_agent_error() -> None:
    target = _http(lambda _: httpx2.Response(200, text="not sse"), stream="sse")
    with pytest.raises(AgentError, match="SSEError"):
        await _reply(target)


@pytest.mark.parametrize("stream", ["none", "sse", "chunked"])
async def test_http_error_status_is_an_agent_error_quoting_the_body(stream: str) -> None:
    target = _http(lambda _: httpx2.Response(503, text="overloaded"), stream=stream)
    with pytest.raises(AgentError, match="HTTP 503: overloaded"):
        await _reply(target)


async def test_http_invalid_json_and_missing_path_are_agent_errors() -> None:
    with pytest.raises(AgentError, match="invalid JSON"):
        await _reply(_http(lambda _: httpx2.Response(200, text="{oops"), response_path="a"))
    with pytest.raises(AgentError, match="has no 'a'"):
        await _reply(_http(lambda _: httpx2.Response(200, json={"b": 1}), response_path="a"))


async def test_http_unreachable_agent_is_an_agent_error() -> None:
    def refuse(request: httpx2.Request) -> httpx2.Response:
        raise httpx2.ConnectError("connection refused", request=request)

    with pytest.raises(AgentError, match="ConnectError"):
        await _reply(_http(refuse))


def test_http_bad_template_or_path_fails_at_construction() -> None:
    with pytest.raises(AdapterConfigError):
        _http(lambda _: httpx2.Response(200), request_template={"q": "{{nope}}"})
    with pytest.raises(AdapterConfigError):
        _http(lambda _: httpx2.Response(200), response_path="a..b")


# --- openai-compatible -------------------------------------------------------------------------


async def test_openai_sends_history_model_and_bearer_key() -> None:
    recorder = Recorder(
        httpx2.Response(
            200, json={"choices": [{"message": {"role": "assistant", "content": "yo"}}]}
        )
    )
    target = _openai(recorder, {"AGENT_KEY": "sk-test"}, api_key_env="AGENT_KEY", stream=False)
    session = await target.open(CTX)
    for text in ("one", "two"):
        _ = [chunk async for chunk in session.send(text)]
    await session.close()

    request = recorder.requests[-1]
    assert str(request.url) == "http://agent.test/v1/chat/completions"
    assert request.headers["Authorization"] == "Bearer sk-test"
    assert request.headers[TRIAL_ID_HEADER] == CTX.trial_id
    assert recorder.body == {
        "messages": [
            {"role": "user", "content": "one"},
            {"role": "assistant", "content": "yo"},
            {"role": "user", "content": "two"},
        ],
        "stream": False,
    }


def test_openai_missing_api_key_variable_fails_at_construction() -> None:
    with pytest.raises(AdapterConfigError, match="AGENT_KEY"):
        _openai(lambda _: httpx2.Response(200), {}, api_key_env="AGENT_KEY")


async def test_openai_null_content_reads_as_an_empty_reply() -> None:
    body = {"choices": [{"message": {"role": "assistant", "content": None}}]}
    chunks = await _reply(_openai(lambda _: httpx2.Response(200, json=body), stream=False))
    assert "".join(c.text for c in chunks) == ""


@pytest.mark.parametrize(
    ("body", "match"),
    [
        ({"error": {"message": "rate limited"}}, "reported an error"),
        ({"choices": [{"message": "text"}]}, "not an object"),
        ({"choices": [{"message": {"content": 3}}]}, "int, not text"),
        ({"result": "x"}, "has no 'choices.0.message'"),
    ],
)
async def test_openai_malformed_completions_are_agent_errors(body: Any, match: str) -> None:
    with pytest.raises(AgentError, match=match):
        await _reply(_openai(lambda _: httpx2.Response(200, json=body), stream=False))


async def test_openai_error_event_mid_stream_is_an_agent_error() -> None:
    body = 'data: {"choices": [{"delta": {"content": "a"}}]}\n\ndata: {"error": "boom"}\n\n'
    with pytest.raises(AgentError, match="boom"):
        await _reply(_openai(lambda _: _sse(body)))
