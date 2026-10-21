"""The ``websocket`` voice adapter's edges: configuration, connection failures, marks, closes."""

import asyncio
import base64
import json
import socket
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager

import anyio
import numpy as np
import pytest
from pydantic import ValidationError
from websockets.asyncio.server import ServerConnection, serve
from websockets.exceptions import ConnectionClosedError
from websockets.protocol import State

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_adapters.voice.websocket import WebSocketAudioLink
from hecklr_schemas import TRIAL_ID_HEADER, SessionContext, TrialId, new_id

pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

Handler = Callable[[ServerConnection], Awaitable[None]]
SILENT_FRAME = bytes(640)


def ctx() -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1, timeout_s=2.0)


def config(url: str, **fields: object) -> WebSocketVoiceAdapterConfig:
    return WebSocketVoiceAdapterConfig.model_validate({"type": "websocket", "url": url, **fields})


@asynccontextmanager
async def agent(handler: Handler) -> AsyncIterator[str]:
    async with serve(handler, "127.0.0.1", 0) as server:
        yield f"ws://127.0.0.1:{next(iter(server.sockets)).getsockname()[1]}/"


def mulaw_media(ms: int) -> str:
    payload = base64.b64encode(bytes([0x80]) * (8 * ms)).decode()  # loud constant samples
    return json.dumps({"event": "media", "streamSid": "MZ", "media": {"payload": payload}})


def test_config_defaults_and_twilio_is_always_8khz() -> None:
    assert config("ws://a").wire_rate == 16_000
    assert config("ws://a", sample_rate=24_000).wire_rate == 24_000
    assert config("ws://a", protocol="twilio").wire_rate == 8_000
    with pytest.raises(ValidationError, match="8 kHz"):
        config("ws://a", protocol="twilio", sample_rate=16_000)
    with pytest.raises(ValidationError):
        config("http://a")


async def test_unreachable_agent_is_an_agent_error() -> None:
    with socket.socket() as probe:  # a port that was free a moment ago, with nobody on it
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    with pytest.raises(AgentError, match="cannot connect"):
        await WebSocketVoiceTarget(config(f"ws://127.0.0.1:{port}/")).connect(ctx())


async def test_agent_that_never_completes_the_handshake_times_out() -> None:
    async def mute(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        await asyncio.sleep(2)
        writer.close()

    server = await asyncio.start_server(mute, "127.0.0.1", 0)
    port = server.sockets[0].getsockname()[1]
    try:
        target = WebSocketVoiceTarget(config(f"ws://127.0.0.1:{port}/", connect_timeout_s=0.2))
        with pytest.raises(AgentTimeoutError, match="in time"):
            await target.connect(ctx())
    finally:
        server.close()


async def test_handshake_carries_the_trial_header_and_configured_headers() -> None:
    seen: dict[str, str | None] = {}

    async def handler(connection: ServerConnection) -> None:
        assert connection.request is not None
        seen["trial"] = connection.request.headers.get(TRIAL_ID_HEADER)
        seen["auth"] = connection.request.headers.get("Authorization")
        await connection.recv()

    async with agent(handler) as url:
        context = ctx()
        target = WebSocketVoiceTarget(config(url, headers={"Authorization": "Bearer t"}))
        link = await target.connect(context)
        await link.hangup()
    assert seen == {"trial": context.trial_id, "auth": "Bearer t"}


async def test_twilio_marks_come_back_when_played_or_cleared() -> None:
    marks: list[tuple[str, float]] = []
    done = anyio.Event()

    async def handler(connection: ServerConnection) -> None:
        await connection.recv()  # connected
        await connection.recv()  # start
        await connection.send(mulaw_media(40))
        await connection.send(json.dumps({"event": "mark", "mark": {"name": "short"}}))
        await connection.send(mulaw_media(2000))
        await connection.send(json.dumps({"event": "mark", "mark": {"name": "long"}}))
        start = anyio.current_time()
        while len(marks) < 1:
            data = json.loads(await connection.recv())
            if data["event"] == "mark":
                marks.append((data["mark"]["name"], anyio.current_time() - start))
        await connection.send(json.dumps({"event": "clear"}))
        while len(marks) < 2:
            data = json.loads(await connection.recv())
            if data["event"] == "mark":
                marks.append((data["mark"]["name"], anyio.current_time() - start))
        done.set()
        await connection.close()

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url, protocol="twilio")).connect(ctx())
        with anyio.fail_after(5):
            frames = [frame async for frame in link.frames()]
            await done.wait()
        await link.hangup()
    assert [name for name, _ in marks] == ["short", "long"]
    assert marks[0][1] < 0.1  # the 40 ms clip played, then its mark came back
    assert marks[1][1] < 0.2  # cleared long before its 2 s of audio could play
    heard_ms = sum(len(frame.pcm) // 2 for frame in frames) / 16
    assert heard_ms < 200


async def test_abnormal_close_mid_call_is_an_agent_error() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.close(code=1011)

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url)).connect(ctx())
        with pytest.raises(AgentError, match="connection failed"), anyio.fail_after(5):
            async for _ in link.frames():
                pass
        await link.hangup()


async def test_after_the_agent_hangs_up_caller_frames_go_nowhere() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.recv()
        await connection.send(np.full(320, 2000, dtype="<i2").tobytes())
        await connection.close()

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url)).connect(ctx())
        with anyio.fail_after(5):
            frames = [frame async for frame in link.frames()]
        assert len(frames) == 1
        await link.send_frame(SILENT_FRAME, 16_000)  # no error: the line is just dead
        await link.hangup()


async def test_a_pipecat_agent_that_sends_text_breaks_the_protocol() -> None:
    async def handler(connection: ServerConnection) -> None:
        await connection.send("not a protobuf frame")
        await connection.wait_closed()

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url, protocol="pipecat")).connect(ctx())
        with pytest.raises(AgentError, match="protobuf"), anyio.fail_after(5):
            async for _ in link.frames():
                pass
        await link.hangup()


async def _assert_released(link: WebSocketAudioLink, closed: anyio.Event) -> None:
    """The socket is closed on both ends and the reading task has finished."""
    with anyio.fail_after(5):
        await closed.wait()
    assert link._socket.state is State.CLOSED
    assert link._receiver._task is not None
    assert link._receiver._task.done()


async def test_hangup_releases_the_socket_when_the_goodbye_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    closed = anyio.Event()

    async def handler(connection: ServerConnection) -> None:
        await connection.wait_closed()
        closed.set()

    async def broken_send(message: str | bytes) -> None:
        raise ConnectionClosedError(None, None)

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url, protocol="twilio")).connect(ctx())
        assert isinstance(link, WebSocketAudioLink)
        monkeypatch.setattr(link._socket, "send", broken_send)
        with pytest.raises(AgentError, match="connection failed"):
            await link.hangup()
        await _assert_released(link, closed)
        await link.hangup()  # already released: nothing left to do, no error


async def test_a_cancelled_hangup_still_releases_the_socket() -> None:
    closed = anyio.Event()

    async def handler(connection: ServerConnection) -> None:
        await connection.wait_closed()
        closed.set()

    async with agent(handler) as url:
        link = await WebSocketVoiceTarget(config(url)).connect(ctx())
        assert isinstance(link, WebSocketAudioLink)
        with anyio.CancelScope() as scope:
            scope.cancel()  # as when the driver's hang-up bound runs out mid-goodbye
            await link.hangup()
        await _assert_released(link, closed)
