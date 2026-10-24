"""Issue #115: header values read from the environment (``headers_env``, RFC 0023)."""

from collections.abc import Callable, Mapping

import httpx2
import pytest
from websockets.asyncio.server import ServerConnection, serve
from websockets.http11 import Request

from hecklr_adapters import (
    AdapterConfigError,
    HttpChatTarget,
    WebSocketChatTarget,
    WebSocketVoiceAdapterConfig,
    make_voice_target,
    resolve_headers,
)
from hecklr_adapters.email import WebhookEmailTarget
from hecklr_adapters.voice import WebSocketVoiceTarget
from hecklr_schemas import (
    EmailMessage,
    HttpAdapterConfig,
    SessionContext,
    TrialId,
    WebhookAdapterConfig,
    WebSocketAdapterConfig,
    new_id,
)

pytestmark = pytest.mark.anyio

TOKEN = "Bearer s3cr3t-token"
ENV = {"AGENT_AUTH_HEADER": TOKEN, "EMPTY": ""}


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1)


def test_issue115_resolve_headers_reads_each_variable() -> None:
    headers = resolve_headers(
        {"X-Team": "qa"}, {"Authorization": "AGENT_AUTH_HEADER"}, environ=ENV, adapter="http"
    )
    assert headers == {"X-Team": "qa", "Authorization": TOKEN}


@pytest.mark.parametrize("variable", ["MISSING", "EMPTY"])
def test_issue115_an_unset_variable_fails_naming_it_not_any_value(variable: str) -> None:
    with pytest.raises(AdapterConfigError) as caught:
        resolve_headers({}, {"Authorization": variable}, environ=ENV, adapter="http")
    message = str(caught.value)
    assert variable in message
    assert "Authorization" in message
    assert "s3cr3t" not in message


def test_issue115_a_header_set_both_ways_is_refused() -> None:
    with pytest.raises(AdapterConfigError, match="both headers and headers_env"):
        resolve_headers(
            {"authorization": "x"},
            {"Authorization": "AGENT_AUTH_HEADER"},
            environ=ENV,
            adapter="http",
        )


async def test_issue115_the_http_agent_receives_the_header_from_the_environment() -> None:
    seen: list[str | None] = []

    def agent(request: httpx2.Request) -> httpx2.Response:
        seen.append(request.headers.get("authorization"))
        return httpx2.Response(200, json={"reply": "hi"})

    config = HttpAdapterConfig.model_validate(
        {
            "type": "http",
            "url": "http://agent.test/chat",
            "headers_env": {"Authorization": "AGENT_AUTH_HEADER"},
            "response_path": "reply",
        }
    )
    target = HttpChatTarget(config, transport=httpx2.MockTransport(agent), environ=ENV)
    session = await target.open(ctx())
    chunks = [chunk.text async for chunk in session.send("hello")]
    await session.close()
    assert "".join(chunks) == "hi"
    assert seen == [TOKEN]
    # Nothing the run stores holds the value: the config still names only the variable.
    assert "s3cr3t" not in config.model_dump_json()


def test_issue115_every_header_adapter_fails_before_any_trial_when_unset() -> None:
    names = {"Authorization": "AGENT_AUTH_HEADER"}
    http = HttpAdapterConfig.model_validate(
        {"type": "http", "url": "http://agent.test/chat", "headers_env": names}
    )
    websocket = WebSocketAdapterConfig.model_validate(
        {"type": "websocket", "url": "ws://agent.test/chat", "headers_env": names}
    )
    webhook = WebhookAdapterConfig.model_validate(
        {"type": "webhook", "url": "http://agent.test/inbound", "headers_env": names}
    )
    voice = WebSocketVoiceAdapterConfig(
        type="websocket", url="ws://agent.test/call", headers_env=names
    )
    builders: list[Callable[[], object]] = [
        lambda: HttpChatTarget(http, environ={}),
        lambda: WebSocketChatTarget(websocket, environ={}),
        lambda: WebhookEmailTarget(webhook, environ={}),
        lambda: WebSocketVoiceTarget(voice, environ={}),
        lambda: make_voice_target(voice, environ={}),
    ]
    for build in builders:
        with pytest.raises(AdapterConfigError, match="AGENT_AUTH_HEADER"):
            build()


async def test_issue115_the_webhook_posts_the_header_from_the_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    seen: list[Mapping[str, str]] = []

    def agent(request: httpx2.Request) -> httpx2.Response:
        seen.append(dict(request.headers))
        return httpx2.Response(202)

    config = WebhookAdapterConfig.model_validate(
        {
            "type": "webhook",
            "url": "http://agent.test/inbound",
            "listen_port": 1,
            "headers_env": {"X-Api-Key": "AGENT_AUTH_HEADER"},
        }
    )
    target = WebhookEmailTarget(config, transport=httpx2.MockTransport(agent), environ=ENV)
    monkeypatch.setattr(target, "start", lambda: None)  # no reply endpoint is needed here
    message = EmailMessage(
        message_id=f"<{new_id()}@c.test>",
        subject="Hi",
        body="Hello",
        from_addr="c@customers.hecklr.test",
        to_addrs=["support@acme.test"],
    )
    await target.deliver(message, ctx())
    assert seen[0]["x-api-key"] == TOKEN


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_issue115_the_websocket_handshake_carries_the_header_from_the_environment() -> None:
    seen: list[str | None] = []

    async def record(connection: ServerConnection, request: Request) -> None:
        seen.append(request.headers.get("Authorization"))

    async def echo(connection: ServerConnection) -> None:
        async for message in connection:
            await connection.send(message)

    names = {"Authorization": "AGENT_AUTH_HEADER"}
    async with serve(echo, "127.0.0.1", 0, process_request=record) as server:
        url = f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}/ws"
        chat = WebSocketAdapterConfig.model_validate(
            {"type": "websocket", "url": url, "headers_env": names}
        )
        session = await WebSocketChatTarget(chat, environ=ENV).open(ctx())
        await session.close()
        voice = WebSocketVoiceAdapterConfig(type="websocket", url=url, headers_env=names)
        link = await WebSocketVoiceTarget(voice, environ=ENV).connect(ctx())
        await link.hangup()
    assert seen == [TOKEN, TOKEN]
