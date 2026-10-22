"""The loopback agent behind a WebSocket, speaking the ``pcm`` voice protocol.

The protocol is the one Hecklr's ``websocket`` voice adapter speaks (``protocol: pcm``): the
caller opens with a JSON ``start`` message (``sample_rate`` must be 16000), then sends binary
16-bit little-endian mono PCM and a JSON ``stop`` before closing. The agent sends binary PCM
back.

Replies go out as 20 ms packets in real time, each sent up to ``LEAD_MS`` before it is due:
that is the sender's buffer, so a scheduling hiccup shorter than it never opens a gap in what
the caller hears. A barge-in cut is decided when the caller's first speech sample arrives,
``barge_in_reaction_ms`` ahead of the cut, so the packet carrying the cut has not left yet and
the stop is placed at the sample.

``GET /healthz`` answers with JSON: the settings, call counts, the process's CPU time, and how
late (worst case) the agent's replies actually left, so a calibration run can tell the agent's
own lateness from Hecklr's measurement error.
"""

import json
import os
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass
from http import HTTPStatus
from typing import Final

import anyio
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from hecklr_voice.audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, Pcm16, bytes_to_pcm16
from hecklr_voice.testing import synthetic_reply
from loopback_agent.call import LoopbackCall, LoopbackSettings, Reply, duration_ms

LEAD_MS: Final = 40.0
"""How far ahead of its play time each reply packet is sent."""
MIN_REACTION_MS: Final = 100.0
"""Fastest barge-in reaction the agent can place exactly.

A caller onset is known one frame (plus network time) after it happens, and by then up to
``LEAD_MS`` of reply has already left; a cut earlier than that could not be honoured.
"""
UNSUPPORTED_DATA: Final = 1003
"""WebSocket close code for a message the agent cannot use (RFC 6455)."""


class CallerProtocolError(Exception):
    """The caller broke the ``pcm`` protocol; the call is closed with ``UNSUPPORTED_DATA``."""


def now_ms() -> float:
    """The agent's clock: milliseconds on this process's monotonic clock."""
    return time.monotonic_ns() / 1e6


async def sleep_until(at_ms: float) -> None:
    """Sleep until the agent's clock reads ``at_ms``."""
    delay = at_ms - now_ms()
    if delay > 0:
        await anyio.sleep(delay / 1000)


@dataclass(slots=True)
class ServerStats:
    """What ``/healthz`` reports."""

    calls_active: int = 0
    calls_total: int = 0
    replies: int = 0
    cuts: int = 0
    max_late_ms: float = 0.0
    """Worst lateness of a reply packet against the time it is due to play.

    The first packet of a reply leaves at its planned time, the rest up to ``LEAD_MS`` ahead,
    so only a stalled agent is late. What Hecklr must measure is what the agent actually did:
    its injected timing plus this lateness (ADR 0010a)."""


class LoopbackServer:
    """Serves loopback calls with one set of injected timings."""

    def __init__(self, settings: LoopbackSettings) -> None:
        reaction = settings.barge_in_reaction_ms
        if reaction is not None and reaction < MIN_REACTION_MS:
            raise ValueError(f"barge_in_reaction_ms must be at least {MIN_REACTION_MS:.0f} ms")
        self.settings = settings
        self.stats = ServerStats()
        self._reply: Pcm16 = synthetic_reply(settings.reply_text)

    def health(self, connection: ServerConnection, request: Request) -> Response | None:
        """``process_request`` hook: answer ``/healthz`` over plain HTTP, upgrade the rest."""
        if request.path != "/healthz":
            return None
        cpu = os.times()
        body = {
            "status": "ok",
            "settings": asdict(self.settings),
            "stats": asdict(self.stats),
            "cpu_s": cpu.user + cpu.system,
        }
        return connection.respond(HTTPStatus.OK, json.dumps(body) + "\n")

    async def handle(self, connection: ServerConnection) -> None:
        """Run one call until the caller hangs up."""
        self.stats.calls_active += 1
        self.stats.calls_total += 1
        try:
            await self._converse(connection)
        except* CallerProtocolError as group:
            await connection.close(UNSUPPORTED_DATA, str(group.exceptions[0]))
        except* ConnectionClosed:
            pass  # the caller hung up mid-call: nothing left to do
        finally:
            self.stats.calls_active -= 1

    async def _converse(self, connection: ServerConnection) -> None:
        _expect_start(await connection.recv())
        call = LoopbackCall(self.settings, duration_ms(self._reply), started_ms=now_ms())
        scheduled = _Signal()
        async with anyio.create_task_group() as group:
            group.start_soon(self._speak, call, scheduled, connection)
            async for message in connection:
                if isinstance(message, str):
                    if _control(message) == "stop":
                        break
                    continue
                if len(message) % 2:
                    raise CallerProtocolError("audio messages must hold whole 16-bit samples")
                if call.hear(bytes_to_pcm16(message), now_ms()):
                    scheduled.fire()
            group.cancel_scope.cancel()

    async def _speak(self, call: LoopbackCall, scheduled: "_Signal", out: ServerConnection) -> None:
        played = 0
        while True:
            while len(call.replies) <= played:
                await scheduled.wait()
            await self._play(call.replies[played], out)
            played += 1

    async def _play(self, reply: Reply, out: ServerConnection) -> None:
        audio = self._reply
        await sleep_until(reply.start_ms)
        for offset in range(0, len(audio), FRAME_SAMPLES):
            due_ms = reply.start_ms + offset * FRAME_MS / FRAME_SAMPLES
            await sleep_until(max(reply.start_ms, due_ms - LEAD_MS))
            late_ms = now_ms() - due_ms
            self.stats.max_late_ms = max(self.stats.max_late_ms, late_ms)
            if offset == 0:
                reply.late_ms = late_ms
                self.stats.replies += 1
            chunk = audio[offset : offset + FRAME_SAMPLES]
            if reply.cut_ms is not None and reply.cut_ms < due_ms + FRAME_MS:
                keep = max(0, round((reply.cut_ms - due_ms) * SAMPLE_RATE / 1000))
                if keep:
                    await out.send(chunk[:keep].astype("<i2").tobytes())
                self.stats.cuts += 1
                return
            await out.send(chunk.astype("<i2").tobytes())


class _Signal:
    """A reusable wake-up: ``fire`` wakes the current waiter, later waits block again."""

    def __init__(self) -> None:
        self._event = anyio.Event()

    def fire(self) -> None:
        self._event.set()

    async def wait(self) -> None:
        await self._event.wait()
        self._event = anyio.Event()


def _control(message: str) -> str | None:
    try:
        data = json.loads(message)
    except json.JSONDecodeError as exc:
        raise CallerProtocolError(f"malformed JSON control message: {exc}") from exc
    kind = data.get("type") if isinstance(data, dict) else None
    return kind if isinstance(kind, str) else None


def _expect_start(message: str | bytes) -> None:
    if isinstance(message, bytes) or _control(message) != "start":
        raise CallerProtocolError("the call must open with a JSON 'start' message")
    rate = json.loads(message).get("sample_rate", SAMPLE_RATE)
    if rate != SAMPLE_RATE:
        raise CallerProtocolError(f"the loopback agent takes {SAMPLE_RATE} Hz audio, got {rate}")


@asynccontextmanager
async def running_server(
    loopback: LoopbackServer, host: str = "127.0.0.1", port: int = 0
) -> AsyncIterator[Server]:
    """Serve ``loopback`` on ``host:port`` (0 picks a free port) for the duration of the block.

    Compression is off: PCM barely compresses, and deflating 50 packets a second per call costs
    both sides CPU that real-time audio needs (it showed in profiles of 50-call runs).
    """
    async with serve(
        loopback.handle,
        host,
        port,
        process_request=loopback.health,
        max_size=None,
        compression=None,
    ) as server:
        yield server


def bound_port(server: Server) -> int:
    """The TCP port a running server listens on."""
    port: int = next(iter(server.sockets)).getsockname()[1]
    return port
