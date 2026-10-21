"""Chat adapters tell an HTTP agent where its trial's world sandbox is (WS7d, ADR 0007d).

An agent under test must call *its* trial's world, or state assertions grade a world it never
touched. Every request carries the URLs as headers, and request templates may place them in the
body with ``{{world_url}}`` / ``{{world_mcp_url}}``.
"""

import json
from typing import Any, cast

import httpx2
import pytest
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection

from hecklr_adapters import (
    WORLD_MCP_URL_HEADER,
    WORLD_URL_HEADER,
    HttpChatTarget,
    OpenAICompatibleChatTarget,
    WebSocketChatSession,
    session_headers,
)
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    HttpAdapterConfig,
    OpenAICompatibleAdapterConfig,
    SessionContext,
    TrialId,
    WebSocketAdapterConfig,
    new_id,
)

pytestmark = pytest.mark.anyio

TRIAL = TrialId(new_id())
WORLD_URL = f"http://127.0.0.1:5123/t/{TRIAL}"
WITH_WORLD = SessionContext(
    trial_id=TRIAL, seed=1, world_http_url=WORLD_URL, world_mcp_url=f"{WORLD_URL}/mcp"
)
WITHOUT_WORLD = SessionContext(trial_id=TRIAL, seed=1)


class Recorder:
    def __init__(self, response: httpx2.Response) -> None:
        self.response = response
        self.requests: list[httpx2.Request] = []

    def __call__(self, request: httpx2.Request) -> httpx2.Response:
        self.requests.append(request)
        return self.response


def _http(recorder: Recorder, **fields: Any) -> HttpChatTarget:
    config = HttpAdapterConfig.model_validate(
        {"type": "http", "url": "http://agent.test/chat", **fields}
    )
    return HttpChatTarget(config, transport=httpx2.MockTransport(recorder))


async def _converse(target: HttpChatTarget | OpenAICompatibleChatTarget, ctx: Any) -> None:
    session = await target.open(ctx)
    _ = [chunk async for chunk in session.send("hello")]
    await session.close()


def test_session_headers_carry_the_trial_and_its_world_urls() -> None:
    assert session_headers(WITH_WORLD) == {
        TRIAL_ID_HEADER: TRIAL,
        WORLD_URL_HEADER: WORLD_URL,
        WORLD_MCP_URL_HEADER: f"{WORLD_URL}/mcp",
    }
    assert session_headers(WITHOUT_WORLD) == {TRIAL_ID_HEADER: TRIAL}


@pytest.mark.parametrize(
    ("stream", "response"),
    [
        ("none", httpx2.Response(200, text="ok")),
        (
            "sse",
            httpx2.Response(
                200, headers={"content-type": "text/event-stream"}, text="data: ok\n\n"
            ),
        ),
        ("chunked", httpx2.Response(200, text="ok")),
    ],
)
async def test_ws7d_http_adapter_sends_the_world_url_headers(
    stream: str, response: httpx2.Response
) -> None:
    recorder = Recorder(response)
    await _converse(_http(recorder, stream=stream), WITH_WORLD)
    headers = recorder.requests[-1].headers
    assert headers[WORLD_URL_HEADER] == WORLD_URL
    assert headers[WORLD_MCP_URL_HEADER] == f"{WORLD_URL}/mcp"
    assert headers[TRIAL_ID_HEADER] == TRIAL


async def test_ws7d_http_template_places_the_world_urls_in_the_body() -> None:
    recorder = Recorder(httpx2.Response(200, text="ok"))
    template = {
        "message": "{{message}}",
        "world_url": "{{world_url}}",
        "tools": {"mcp": "{{world_mcp_url}}"},
    }
    await _converse(_http(recorder, request_template=template), WITH_WORLD)
    assert json.loads(recorder.requests[-1].content) == {
        "message": "hello",
        "world_url": WORLD_URL,
        "tools": {"mcp": f"{WORLD_URL}/mcp"},
    }


async def test_ws7d_without_a_world_the_headers_are_absent_and_placeholders_empty() -> None:
    recorder = Recorder(httpx2.Response(200, text="ok"))
    template = {"message": "{{message}}", "world_url": "{{world_url}}"}
    await _converse(_http(recorder, request_template=template), WITHOUT_WORLD)
    request = recorder.requests[-1]
    assert WORLD_URL_HEADER not in request.headers
    assert WORLD_MCP_URL_HEADER not in request.headers
    assert json.loads(request.content) == {"message": "hello", "world_url": ""}


async def test_ws7d_openai_compatible_adapter_sends_the_world_url_headers() -> None:
    recorder = Recorder(httpx2.Response(200, json={"choices": [{"message": {"content": "ok"}}]}))
    config = OpenAICompatibleAdapterConfig.model_validate(
        {"type": "openai-compatible", "url": "http://agent.test/v1", "stream": False}
    )
    target = OpenAICompatibleChatTarget(config, transport=httpx2.MockTransport(recorder))
    await _converse(target, WITH_WORLD)
    assert recorder.requests[-1].headers[WORLD_URL_HEADER] == WORLD_URL


class ScriptedSocket:
    def __init__(self) -> None:
        self.sent: list[str] = []

    async def send(self, message: str) -> None:
        self.sent.append(message)

    async def recv(self, decode: bool) -> str:
        return "ok"


async def test_ws7d_websocket_template_places_the_world_url_in_the_message() -> None:
    config = WebSocketAdapterConfig(type="websocket", url="ws://agent.test/ws")
    socket = ScriptedSocket()
    template: JsonValue = {"m": "{{message}}", "world": "{{world_url}}"}
    session = WebSocketChatSession(config, template, WITH_WORLD, cast(ClientConnection, socket))
    _ = [chunk async for chunk in session.send("hello")]
    assert json.loads(socket.sent[-1]) == {"m": "hello", "world": WORLD_URL}
