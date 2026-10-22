"""Voice and email adapters tell the agent where its trial's world sandbox is (ADR 0007e).

Chat adapters forward the world URLs since ADR 0007d. A voice or email agent must find its
trial's world too, or a voice or email trial's state assertions grade a world the agent never
touched. Each transport carries them where its agents can read them: headers on HTTP and
WebSocket requests and on SMTP mail, and ``session_fields`` in the transport's own data.
"""

import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from email.message import EmailMessage as MimeMessage
from typing import Any

import httpx2
import pytest
from websockets.asyncio.server import ServerConnection, serve

from hecklr_adapters import (
    WORLD_MCP_URL_HEADER,
    WORLD_URL_HEADER,
    AgentError,
    session_fields,
)
from hecklr_adapters.email import SmtpImapEmailTarget, WebhookEmailTarget
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_adapters.voice.livekit import LiveKitVoiceTarget, participant_attributes
from hecklr_adapters.voice.pipecat import PipecatVoiceTarget
from hecklr_adapters.voice.wire import PcmWire, TwilioWire
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    EmailMessage,
    LiveKitAdapterConfig,
    PipecatAdapterConfig,
    SessionContext,
    SmtpImapAdapterConfig,
    TrialId,
    WebhookAdapterConfig,
    new_id,
)

TRIAL = TrialId(new_id())
WORLD_URL = f"http://127.0.0.1:5123/t/{TRIAL}"
MCP_URL = f"{WORLD_URL}/mcp"
WITH_WORLD = SessionContext(
    trial_id=TRIAL, seed=1, world_http_url=WORLD_URL, world_mcp_url=MCP_URL, timeout_s=2.0
)
WITHOUT_WORLD = SessionContext(trial_id=TRIAL, seed=1, timeout_s=2.0)
FIELDS = {"trial_id": TRIAL, "world_url": WORLD_URL, "world_mcp_url": MCP_URL}
HEADERS = {TRIAL_ID_HEADER: TRIAL, WORLD_URL_HEADER: WORLD_URL, WORLD_MCP_URL_HEADER: MCP_URL}


def test_ws_wire_session_fields_carry_the_trial_and_only_the_world_it_has() -> None:
    assert session_fields(WITH_WORLD) == FIELDS
    assert session_fields(WITHOUT_WORLD) == {"trial_id": TRIAL}


# --- voice -----------------------------------------------------------------------------------


def test_ws_wire_pcm_start_message_names_the_world() -> None:
    [start] = PcmWire(WITH_WORLD, "s1", 16_000).opening()
    assert isinstance(start, str)
    assert json.loads(start) == {
        "type": "start",
        "session_id": "s1",
        **FIELDS,
        "sample_rate": 16_000,
        "encoding": "pcm_s16le",
        "channels": 1,
    }


def test_ws_wire_twilio_custom_parameters_name_the_world() -> None:
    _, start = TwilioWire(WITH_WORLD, "s1").opening()
    assert isinstance(start, str)
    assert json.loads(start)["start"]["customParameters"] == FIELDS
    _, bare = TwilioWire(WITHOUT_WORLD, "s1").opening()
    assert isinstance(bare, str)
    assert json.loads(bare)["start"]["customParameters"] == {"trial_id": TRIAL}


@pytest.mark.anyio
@pytest.mark.parametrize("protocol", ["pcm", "twilio", "pipecat"])
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws_wire_websocket_voice_handshake_carries_the_world_headers(protocol: str) -> None:
    seen: dict[str, str | None] = {}

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        seen.update({name: connection.request.headers.get(name) for name in HEADERS})
        async for _ in connection:
            pass

    async with serve(handler, "127.0.0.1", 0) as server:
        port = next(iter(server.sockets)).getsockname()[1]
        config = WebSocketVoiceAdapterConfig.model_validate(
            {"type": "websocket", "url": f"ws://127.0.0.1:{port}/", "protocol": protocol}
        )
        link = await WebSocketVoiceTarget(config).connect(WITH_WORLD)
        await link.hangup()
    assert seen == HEADERS


@asynccontextmanager
async def capturing_http_agent(
    requests: list[tuple[dict[str, str], Any]], status: int = 500
) -> AsyncIterator[str]:
    """An HTTP endpoint that records each request's headers and JSON body, then fails it."""

    async def handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        head = (await reader.readuntil(b"\r\n\r\n")).decode()
        headers = {
            name.strip().lower(): value.strip()
            for name, _, value in (line.partition(":") for line in head.split("\r\n")[1:])
            if name
        }
        body = await reader.readexactly(int(headers["content-length"]))
        requests.append((headers, json.loads(body)))
        writer.write(
            f"HTTP/1.1 {status} X\r\nContent-Length: 0\r\nConnection: close\r\n\r\n".encode()
        )
        await writer.drain()
        writer.close()

    server = await asyncio.start_server(handle, "127.0.0.1", 0)
    try:
        yield f"http://127.0.0.1:{server.sockets[0].getsockname()[1]}/api/offer"
    finally:
        server.close()


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws_wire_pipecat_offer_carries_the_world_in_headers_and_request_data() -> None:
    requests: list[tuple[dict[str, str], Any]] = []
    async with capturing_http_agent(requests) as url:
        config = PipecatAdapterConfig.model_validate({"type": "pipecat", "url": url})
        target = PipecatVoiceTarget(config)
        with pytest.raises(AgentError, match="HTTP 500"):
            await target.connect(WITH_WORLD)
    [(headers, body)] = requests
    assert body["request_data"] == FIELDS
    assert {name: headers[name.lower()] for name in HEADERS} == HEADERS


def _jwt_claims(token: str) -> dict[str, Any]:
    payload = token.split(".")[1]
    padded = payload + "=" * (-len(payload) % 4)
    claims: dict[str, Any] = json.loads(base64.urlsafe_b64decode(padded))
    return claims


def test_ws_wire_livekit_token_names_the_world_in_metadata_and_attributes() -> None:
    config = LiveKitAdapterConfig(type="livekit", url="ws://127.0.0.1:7880")
    target = LiveKitVoiceTarget(config, {"LIVEKIT_API_KEY": "k", "LIVEKIT_API_SECRET": "s" * 32})
    claims = _jwt_claims(target._token(WITH_WORLD))
    assert json.loads(claims["metadata"]) == FIELDS
    assert claims["attributes"] == {
        "hecklr.trial_id": TRIAL,
        "hecklr.world_url": WORLD_URL,
        "hecklr.world_mcp_url": MCP_URL,
    }
    assert participant_attributes(WITHOUT_WORLD) == {"hecklr.trial_id": TRIAL}


# --- email -----------------------------------------------------------------------------------


def _email() -> EmailMessage:
    return EmailMessage(
        message_id=f"<{new_id()}@customers.hecklr.test>",
        subject="Visit",
        body="Move my visit",
        from_addr="c.1@customers.hecklr.test",
        to_addrs=["support@acme.test"],
    )


@pytest.mark.anyio
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws_wire_email_webhook_posts_the_world_in_headers_and_body() -> None:
    posted: list[httpx2.Request] = []

    def agent(request: httpx2.Request) -> httpx2.Response:
        posted.append(request)
        return httpx2.Response(200)

    with socket.socket() as probe:  # the reply endpoint starts on delivery: give it a free port
        probe.bind(("127.0.0.1", 0))
        port = int(probe.getsockname()[1])
    config = WebhookAdapterConfig.model_validate(
        {"type": "webhook", "url": "http://agent.test/inbound", "listen_port": port}
    )
    target = WebhookEmailTarget(config, transport=httpx2.MockTransport(agent))
    try:
        await target.deliver(_email(), WITH_WORLD)
    finally:
        target.close()
    [request] = posted
    assert {name: request.headers[name] for name in HEADERS} == HEADERS
    body = json.loads(request.content)
    assert {name: body[name] for name in FIELDS} == FIELDS


@pytest.fixture
def sent_mail(monkeypatch: pytest.MonkeyPatch) -> list[MimeMessage]:
    """Replaces ``smtplib.SMTP`` with a server that keeps every message it is sent."""
    sent: list[MimeMessage] = []

    class Smtp:
        def __init__(self, *args: object, **kwargs: object) -> None:
            pass

        def __enter__(self) -> "Smtp":
            return self

        def __exit__(self, *args: object) -> None:
            pass

        def send_message(self, message: MimeMessage) -> None:
            sent.append(message)

    monkeypatch.setattr("smtplib.SMTP", Smtp)
    return sent


@pytest.mark.anyio
async def test_ws_wire_smtp_mail_carries_the_world_headers(sent_mail: list[MimeMessage]) -> None:
    config = SmtpImapAdapterConfig(type="smtp-imap", inbox="support@acme.test")
    await SmtpImapEmailTarget(config).deliver(_email(), WITH_WORLD)
    [mime] = sent_mail
    assert {name: mime[name] for name in HEADERS} == HEADERS
    await SmtpImapEmailTarget(config).deliver(_email(), WITHOUT_WORLD)
    assert sent_mail[1][WORLD_URL_HEADER] is None
