"""The ``websocket`` voice adapter: one socket per call, raw PCM, Twilio or Pipecat messages.

Caller frames are converted to the wire's rate and format and sent as they come (the driver
paces them). Agent audio goes through a :class:`~hecklr_adapters.voice.playout.Playout`, so an
agent that bursts its reply is heard in real time, and a ``clear`` (or Pipecat interruption)
cuts it off where playback had got to. A normal close from the agent is a hang-up; an abnormal
one, or a protocol violation, is an ``AgentError``.
"""

from collections.abc import AsyncIterator
from typing import Final

import anyio
import anyio.lowlevel
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosedError, ConnectionClosedOK, WebSocketException

from hecklr_adapters.errors import AgentError, AgentTimeoutError
from hecklr_adapters.voice.audio import SAMPLE_RATE, StreamResampler, ToHarness, pcm16
from hecklr_adapters.voice.config import WebSocketVoiceAdapterConfig
from hecklr_adapters.voice.playout import Mark, Playout, Receiver
from hecklr_adapters.voice.wire import (
    AgentAudio,
    AgentClear,
    AgentMark,
    Message,
    PcmWire,
    PipecatWire,
    TwilioWire,
    Wire,
)
from hecklr_schemas import SessionContext, TimedFrame, new_id

CLOSE_TIMEOUT_S: Final = 1.0
"""How long the closing handshake may wait on the agent before the connection is dropped.

websockets bounds ``close()`` by this itself (and advises against cancelling ``close()``), so a
hang-up ends within a few seconds even when the agent never answers the close.
"""


def check_caller_rate(sample_rate: int) -> None:
    """Caller frames come from the harness at 16 kHz; anything else is a harness bug."""
    if sample_rate != SAMPLE_RATE:
        raise ValueError(f"voice adapters take {SAMPLE_RATE} Hz caller frames, got {sample_rate}")


class WebSocketVoiceTarget:
    """Places each call over a new WebSocket connection."""

    def __init__(self, config: WebSocketVoiceAdapterConfig) -> None:
        self._config = config

    async def connect(self, ctx: SessionContext) -> "WebSocketAudioLink":
        """Open the socket (carrying the trial header) and start the call."""
        config = self._config
        try:
            socket = await connect(
                config.url,
                additional_headers={**config.headers, **ctx.headers()},
                open_timeout=config.connect_timeout_s,
                close_timeout=CLOSE_TIMEOUT_S,
                max_size=None,
            )
        except TimeoutError as exc:
            raise AgentTimeoutError(f"agent at {config.url} did not answer in time") from exc
        except (OSError, WebSocketException) as exc:
            raise AgentError(f"cannot connect to agent at {config.url}: {exc}") from exc
        link = WebSocketAudioLink(socket, self._wire(ctx), config.wire_rate, ctx.timeout_s)
        await link.start()
        return link

    def _wire(self, ctx: SessionContext) -> Wire:
        session_id = new_id()
        match self._config.protocol:
            case "pcm":
                return PcmWire(ctx, session_id, self._config.wire_rate)
            case "twilio":
                return TwilioWire(ctx, session_id)
            case "pipecat":
                return PipecatWire(self._config.wire_rate)


class WebSocketAudioLink:
    """One call over an open WebSocket."""

    def __init__(
        self, socket: ClientConnection, wire: Wire, wire_rate: int, send_timeout_s: float
    ) -> None:
        self._socket = socket
        self._wire = wire
        self._to_wire = StreamResampler(SAMPLE_RATE, wire_rate)
        self._to_harness = ToHarness()
        self._playout = Playout()
        self._receiver = Receiver(self._playout, self._receive)
        self._send_timeout_s = send_timeout_s
        self._send_lock = anyio.Lock()
        self._agent_hung_up = False
        self._hung_up = False

    async def start(self) -> None:
        """Send the format's opening messages and start listening."""
        for message in self._wire.opening():
            await self._send(message)
        self._receiver.start()

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        """Send one caller frame; after the agent hung up, frames go nowhere."""
        check_caller_rate(sample_rate)
        if self._hung_up:
            raise AgentError("the call is over: it was hung up")
        samples = self._to_wire.process(pcm16(pcm))
        if len(samples) and not self._agent_hung_up:
            await self._send(self._wire.audio(samples))

    def frames(self) -> AsyncIterator[TimedFrame]:
        """The agent's audio as heard in real time, until the agent hangs up."""
        return self._frames()

    async def hangup(self) -> None:
        """Say goodbye in the wire's terms and close the socket; safe to call twice.

        The socket and the reading task are released even when the goodbye fails or the
        caller cancels it (the driver bounds hang-ups): a misbehaving agent must not leave a
        connection open behind each trial. ``CLOSE_TIMEOUT_S`` bounds the shielded close.
        """
        if self._hung_up:
            return
        self._hung_up = True
        try:
            if not self._agent_hung_up:
                for message in self._wire.closing():
                    await self._send(message)
        finally:
            with anyio.CancelScope(shield=True):
                try:
                    await self._socket.close()
                finally:
                    await self._receiver.stop()

    async def _frames(self) -> AsyncIterator[TimedFrame]:
        async for frame in self._receiver.frames():
            await self._report_marks(self._playout.played_marks())
            yield frame

    async def _receive(self) -> bool:
        try:
            message = await self._socket.recv()
        except ConnectionClosedOK:
            self._agent_hung_up = True
            return False
        except ConnectionClosedError as exc:
            self._agent_hung_up = True
            if self._hung_up:
                return False
            raise AgentError(f"agent connection failed: {exc}") from exc
        await self._handle(message)
        return True

    async def _handle(self, message: Message) -> None:
        match self._wire.read(message):
            case AgentAudio(samples, rate):
                # A burst can hold seconds of speech: convert it in 100 ms pieces and let other
                # tasks run in between, so the caller's frames keep leaving on time.
                piece = max(1, rate // 10)
                for start in range(0, len(samples), piece):
                    self._playout.write(self._to_harness(samples[start : start + piece], rate))
                    await anyio.lowlevel.checkpoint()
            case AgentClear():
                await self._report_marks(self._playout.clear())
            case AgentMark(name):
                await self._report_marks(self._playout.mark(name))
            case None:
                pass

    async def _report_marks(self, marks: list[Mark]) -> None:
        for mark in marks:
            reply = self._wire.mark_played(mark.name)
            if reply is not None:
                await self._send(reply)

    async def _send(self, message: Message) -> None:
        try:
            with anyio.fail_after(self._send_timeout_s):
                async with self._send_lock:
                    await self._socket.send(message)
        except TimeoutError as exc:
            raise AgentTimeoutError(
                f"agent stopped reading: a send stalled for {self._send_timeout_s} s"
            ) from exc
        except ConnectionClosedOK:
            self._agent_hung_up = True
        except ConnectionClosedError as exc:
            self._agent_hung_up = True
            raise AgentError(f"agent connection failed: {exc}") from exc
