"""The acme voice agent behind a WebSocket, speaking the ``pcm`` voice protocol.

The protocol is the one Hecklr's ``websocket`` voice adapter speaks (``protocol: pcm``, RFC
0010a): the caller opens with a JSON ``start`` message (``sample_rate`` must be 16000), sends
binary 16-bit little-endian mono PCM, and a JSON ``stop`` before closing. The agent sends binary
PCM back, a JSON ``{"type": "clear"}`` when the caller barges in, and closes the socket
normally when it hands the call to a human.

Which world the tools act on, per call (ADR 0007e), the first of:

1. ``world_url`` in the ``start`` message;
2. the ``X-Hecklr-World-Url`` handshake header;
3. ``--world-base-url`` + ``/t/<trial id>`` (``trial_id`` from ``start`` or ``X-Hecklr-Trial-Id``);
4. a private in-process copy of the world, for manual calls. Hecklr cannot see those tool calls.

``GET /healthz`` answers with JSON: the variant, the pipeline settings, call counts and the
calls served from each world source.
"""

import json
import time
from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict
from http import HTTPStatus
from typing import Final, Literal

import anyio
import httpx2
from acme_agent import AcmeAgent, HttpWorldTools, LocalWorldTools, ToolBackend
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from acme_voice.config import PipelineConfig
from acme_voice.pipeline import SpeechStack, VoiceCall
from hecklr_adapters import WORLD_URL_HEADER
from hecklr_schemas import TRIAL_ID_HEADER, World, new_id
from hecklr_voice.audio import SAMPLE_RATE, Pcm16, bytes_to_pcm16

WorldSource = Literal["start", "header", "base_url", "local"]
UNSUPPORTED_DATA: Final = 1003
"""WebSocket close code for a message the agent cannot use (RFC 6455)."""


class CallerProtocolError(Exception):
    """The caller broke the ``pcm`` protocol; the call is closed with ``UNSUPPORTED_DATA``."""


def now_ms() -> float:
    """The agent's clock: milliseconds on this process's monotonic clock."""
    return time.monotonic_ns() / 1e6


class WebSocketMouth:
    """Speech out over the call's socket."""

    def __init__(self, connection: ServerConnection) -> None:
        self._connection = connection

    async def play(self, pcm: Pcm16) -> None:
        """Send the whole clip at once; the caller's side plays it in real time."""
        await self._connection.send(pcm.astype("<i2").tobytes())

    async def clear(self) -> None:
        """Ask the caller's side to drop what it has not played."""
        await self._connection.send(json.dumps({"type": "clear"}))

    async def hang_up(self) -> None:
        """Close the socket normally."""
        await self._connection.close()


class VoiceAgentServer:
    """Serves calls with one agent, one pipeline configuration and one speech stack."""

    def __init__(
        self,
        agent: AcmeAgent,
        world: World,
        config: PipelineConfig,
        stack: SpeechStack,
        client: httpx2.AsyncClient,
        *,
        world_base_url: str | None = None,
    ) -> None:
        self.agent = agent
        self.config = config
        self.calls: list[VoiceCall] = []
        """Every call taken, for inspection in tests and demos."""
        self._world = world
        self._stack = stack
        self._client = client
        self._base_url = world_base_url
        self._served: Counter[WorldSource] = Counter()
        self._active = 0

    def health(self, connection: ServerConnection, request: Request) -> Response | None:
        """``process_request`` hook: answer ``/healthz`` over plain HTTP, upgrade the rest."""
        if request.path != "/healthz":
            return None
        body = {
            "status": "ok",
            "brain": self.agent.settings.brain,
            "variant": self.config.variant,
            "pipeline": asdict(self.config),
            "calls_active": self._active,
            "calls_total": len(self.calls),
            "calls_by_world_source": dict(self._served),
        }
        return connection.respond(HTTPStatus.OK, json.dumps(body) + "\n")

    async def handle(self, connection: ServerConnection) -> None:
        """Run one call until either side hangs up."""
        self._active += 1
        try:
            await self._converse(connection)
        except* CallerProtocolError as group:
            await connection.close(UNSUPPORTED_DATA, str(group.exceptions[0]))
        except* ConnectionClosed:
            pass  # the caller hung up mid-sentence: nothing left to say
        finally:
            self._active -= 1

    async def _converse(self, connection: ServerConnection) -> None:
        start = _start_message(await connection.recv())
        headers = connection.request.headers if connection.request is not None else Headers()
        backend = self._backend(start, headers)
        session_id = _text(start.get("session_id")) or new_id()
        call = VoiceCall(
            self.config,
            self._stack,
            lambda text: self.agent.respond(session_id, text, lambda: backend),
            WebSocketMouth(connection),
            now_ms,
        )
        self.calls.append(call)
        await call.prepare()
        async with anyio.create_task_group() as group:

            async def answer() -> None:
                await call.run()
                group.cancel_scope.cancel()  # the agent hung up

            group.start_soon(answer)
            async for message in connection:
                if isinstance(message, bytes):
                    await call.hear(_pcm(message))
                elif _control(message).get("type") == "stop":
                    break
            group.cancel_scope.cancel()

    def _backend(self, start: dict[str, object], headers: Headers) -> ToolBackend:
        trial_id = _text(start.get("trial_id")) or headers.get(TRIAL_ID_HEADER)
        source: WorldSource
        if world_url := _text(start.get("world_url")):
            source = "start"
        elif world_url := headers.get(WORLD_URL_HEADER):
            source = "header"
        elif self._base_url is not None and trial_id:
            source, world_url = "base_url", f"{self._base_url.rstrip('/')}/t/{trial_id}"
        else:
            source, world_url = "local", None
        self._served[source] += 1
        if world_url is None:
            return LocalWorldTools(self._world)
        return HttpWorldTools(self._client, world_url, trial_id)


def _text(value: object) -> str | None:
    return value if isinstance(value, str) and value else None


def _control(message: str) -> dict[str, object]:
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise CallerProtocolError(f"malformed JSON control message: {exc}") from exc
    if not isinstance(data, dict):
        raise CallerProtocolError("control messages are JSON objects")
    return data


def _start_message(message: str | bytes) -> dict[str, object]:
    start = None if isinstance(message, bytes) else _control(message)
    if start is None or start.get("type") != "start":
        raise CallerProtocolError("the call must open with a JSON 'start' message")
    rate = start.get("sample_rate", SAMPLE_RATE)
    if rate != SAMPLE_RATE:
        raise CallerProtocolError(f"the acme voice agent takes {SAMPLE_RATE} Hz audio, got {rate}")
    return start


def _pcm(message: bytes) -> Pcm16:
    if len(message) % 2:
        raise CallerProtocolError("audio messages must hold whole 16-bit samples")
    return bytes_to_pcm16(message)


@asynccontextmanager
async def running_server(
    server: VoiceAgentServer, host: str = "127.0.0.1", port: int = 0
) -> AsyncIterator[Server]:
    """Serve calls on ``host:port`` (0 picks a free port) for the duration of the block.

    Compression is off: PCM barely compresses, and deflating every packet costs real-time CPU.
    """
    async with serve(
        server.handle,
        host,
        port,
        process_request=server.health,
        max_size=None,
        compression=None,
    ) as running:
        yield running


def bound_port(server: Server) -> int:
    """The TCP port a running server listens on."""
    port: int = next(iter(server.sockets)).getsockname()[1]
    return port
