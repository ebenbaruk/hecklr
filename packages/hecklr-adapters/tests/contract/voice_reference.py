"""Reference voice agent of the voice contract suite: a loopback with known timing.

``LoopbackBrain`` is the transport-free agent. It listens to the caller, and
``reply_after_ms`` after the caller's last speech sample it says a fixed reply; when the caller
speaks over a reply it stops ``reaction_ms`` after the caller's first speech sample. Times are
taken on the monotonic clock at the moment caller audio arrives, so the agent knows the truth
Hecklr must measure (SC4). It is written independently of the adapters under test: audio
conversion here uses ``hecklr-voice``'s offline codec and resampler, and Pipecat's own protobuf
code.

Agents run on their own event loop in a background thread, as a real agent runs in its own
process: the harness's loop stalls (process-pool start-up, garbage collection) must not move
the agent's clock, or the reference truth would be wrong.

``serve_websocket`` puts the brain behind a WebSocket in one of the adapter's three message
formats, speaking either ``paced`` (20 ms packets in real time, stopping on a cut) or ``burst``
(the whole reply at once, then a ``clear`` on a cut), the two rhythms real agents use.
"""

import asyncio
import base64
import contextlib
import functools
import importlib
import json
import threading
import time
from collections.abc import Awaitable, Callable, Coroutine, Iterator
from dataclasses import dataclass, field
from typing import Any, Final, Literal

import anyio
import numpy as np
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from hecklr_schemas import TRIAL_ID_HEADER
from hecklr_voice.audio import SAMPLE_RATE, Pcm16, frame_dbfs, resample, speech_edges, to_pcm16
from hecklr_voice.codec import mulaw_decode, mulaw_encode
from hecklr_voice.vad import DEFAULT_ENERGY_THRESHOLD_DBFS

FRAME_MS: Final = 20
PACED_LEAD_MS: Final = 40
# Pipecat's own generated protobuf code, so the adapter's hand-written codec is checked against
# the real format. Generated code carries no type information.
frames_pb2: Any = importlib.import_module("pipecat.frames.protobufs.frames_pb2")
Rhythm = Literal["paced", "burst"]


def now_ms() -> float:
    """The agent's clock."""
    return time.monotonic_ns() / 1e6


def convert(pcm: Pcm16, rate_in: int, rate_out: int) -> Pcm16:
    """Offline resampling of a whole clip (``hecklr-voice``'s polyphase filter)."""
    if rate_in == rate_out:
        return pcm
    return to_pcm16(resample(pcm.astype(np.float32) / 32768.0, rate_in, rate_out))


async def sleep_until(at_ms: float) -> None:
    """Sleep until the agent's clock reads ``at_ms``."""
    delay = at_ms - now_ms()
    if delay > 0:
        await anyio.sleep(delay / 1000)


async def poll_until(condition: Callable[[], bool], interval_s: float = 0.002) -> None:
    """Wait until ``condition()`` holds.

    The state it reads is written by another thread (the harness or the agent loop), where an
    ``anyio.Event`` cannot be set, so polling is the simple correct wait.
    """
    while not condition():  # noqa: ASYNC110 -- see the docstring
        await anyio.sleep(interval_s)


@dataclass
class Reply:
    """One reply as the agent played it, on its own clock."""

    start_ms: float
    duration_ms: float
    cut_ms: float | None = None
    late_ms: float = 0.0
    """How much later than planned the first audio actually left."""
    cut_late_ms: float = 0.0
    """How much later than planned the cut actually happened.

    The agent's thread competes with the harness for the CPU, so its sends can leave a few
    milliseconds late; the truth Hecklr must measure is what the agent actually did.
    """

    @property
    def end_ms(self) -> float:
        """When the reply stops: its cut, or its natural end."""
        natural = self.start_ms + self.duration_ms
        return natural if self.cut_ms is None else min(natural, self.cut_ms)


@dataclass
class LoopbackBrain:
    """The loopback agent's ears and timing; transports only move audio."""

    reply: Pcm16
    """What the agent says, at 16 kHz."""
    reply_after_ms: float
    reaction_ms: float | None
    endpoint_ms: float = 200.0
    greeting: bool = False
    hang_up_after: int | None = None
    media_clock: bool = True
    """Time caller audio by its sample position (exact on a sample-preserving transport).

    WebRTC receivers stretch and shrink audio to manage their jitter buffer, so positions
    drift from real time there; those shells time each chunk by its arrival instead.
    """
    replies: list[Reply] = field(default_factory=list)
    caller_rate: int | None = None
    speech_ms: list[float] = field(default_factory=list)
    """Times of every caller speech edge heard."""
    _speaking: bool = False
    _last_ms: float = 0.0
    _heard: int = 0
    """Caller samples heard so far: the position on the caller's media clock."""
    _origin_ms: float | None = None

    def __post_init__(self) -> None:
        if self.greeting:
            self.replies.append(Reply(now_ms() + 200.0, self.reply_ms))

    @property
    def reply_ms(self) -> float:
        """Length of the reply."""
        return len(self.reply) / (SAMPLE_RATE / 1000)

    def hear(self, samples: Pcm16, rate: int, arrival_ms: float) -> None:
        """Hear one chunk of caller audio that arrived at ``arrival_ms``.

        Times come from the caller's media clock with earliest-arrival alignment, as an RTP
        receiver does, so this thread's own scheduling delays never move the truth.
        """
        self.caller_rate = rate
        per_ms = rate / 1000
        origin = arrival_ms - self._heard / per_ms
        self._origin_ms = origin if self._origin_ms is None else min(self._origin_ms, origin)
        start_ms = self._origin_ms + self._heard / per_ms if self.media_clock else arrival_ms
        self._heard += len(samples)
        speech = len(samples) > 0 and frame_dbfs(samples) >= DEFAULT_ENERGY_THRESHOLD_DBFS
        edges = speech_edges(samples) if speech else None
        if edges is not None:
            onset_ms, end_ms = start_ms + edges[0] / per_ms, start_ms + edges[1] / per_ms
            self.speech_ms += [onset_ms, end_ms]
            if not self._speaking:
                self._speaking = True
                self._caller_started(onset_ms)
            self._last_ms = end_ms
        elif self._speaking and start_ms - self._last_ms >= self.endpoint_ms:
            self._speaking = False
            busy_until = max((reply.end_ms for reply in self.replies), default=0.0)
            start = max(self._last_ms + self.reply_after_ms, busy_until)
            self.replies.append(Reply(start, self.reply_ms))

    def _caller_started(self, onset_ms: float) -> None:
        if self.reaction_ms is None:
            return
        for reply in self.replies:
            if reply.start_ms <= onset_ms < reply.end_ms:
                reply.cut_ms = onset_ms + self.reaction_ms


# --- speaking --------------------------------------------------------------------------------

Send = Callable[[Pcm16], Awaitable[None]]
"""Send one chunk of reply audio, already at the wire's rate, in the transport's format."""


@dataclass
class Voice:
    """How an agent shell speaks the brain's replies."""

    rhythm: Rhythm
    rate: int
    send: Send
    clear: Callable[[], Awaitable[None]]


async def speak(brain: LoopbackBrain, voice: Voice) -> None:
    """Play every reply the brain schedules, until it decides to hang up."""
    audio = convert(brain.reply, SAMPLE_RATE, voice.rate)
    played = 0
    while brain.hang_up_after is None or played < brain.hang_up_after:
        await poll_until(functools.partial(_scheduled, brain, played))
        await _say(brain.replies[played], audio, voice)
        played += 1
    await sleep_until(brain.replies[-1].end_ms + 300.0)


def _scheduled(brain: LoopbackBrain, count: int) -> bool:
    return len(brain.replies) > count


async def _say(reply: Reply, audio: Pcm16, voice: Voice) -> None:
    await sleep_until(reply.start_ms)
    if voice.rhythm == "paced":
        await _paced(reply, audio, voice)
        return
    # As TTS streams: a burst of chunks far faster than real time, the first one small.
    for offset in range(0, len(audio), voice.rate // 4):
        await voice.send(audio[offset : offset + voice.rate // 4])
        if offset == 0:
            reply.late_ms = now_ms() - reply.start_ms
    await poll_until(lambda: now_ms() >= reply.end_ms, 0.001)
    if reply.cut_ms is not None and reply.cut_ms < reply.start_ms + reply.duration_ms:
        await voice.clear()
        reply.cut_late_ms = now_ms() - reply.cut_ms


async def _paced(reply: Reply, audio: Pcm16, voice: Voice) -> None:
    """Real-time 20 ms packets, each sent up to ``PACED_LEAD_MS`` ahead of its time.

    The lead is the sender's buffer: a scheduling hiccup on this thread shorter than it never
    opens a gap in what the caller hears. The cut is decided at the caller's onset, well
    before its packet leaves, so the stop is still placed at the sample.
    """
    per_frame = voice.rate * FRAME_MS // 1000
    per_ms = voice.rate / 1000
    for offset in range(0, len(audio), per_frame):
        at = reply.start_ms + offset / per_ms
        await sleep_until(max(reply.start_ms, at - PACED_LEAD_MS))
        chunk = audio[offset : offset + per_frame]
        if offset == 0:
            reply.late_ms = now_ms() - at
        if reply.cut_ms is not None and reply.cut_ms < at + FRAME_MS:
            keep = max(0, round((reply.cut_ms - at) * per_ms))
            if keep:
                await voice.send(chunk[:keep])
            # The packets play back to back from the first one's arrival.
            reply.cut_late_ms = reply.late_ms + at + keep / per_ms - reply.cut_ms
            return
        await voice.send(chunk)


# --- a loop of its own -----------------------------------------------------------------------


class AgentThread:
    """An asyncio loop in a daemon thread, where reference agents run."""

    def __init__(self) -> None:
        self.loop = asyncio.new_event_loop()
        self._thread = threading.Thread(target=self.loop.run_forever, daemon=True)
        self._thread.start()

    def run[T](self, coroutine: Coroutine[Any, Any, T], timeout_s: float = 30.0) -> T:
        """Run ``coroutine`` on the agent loop and wait for its result."""
        return asyncio.run_coroutine_threadsafe(coroutine, self.loop).result(timeout_s)

    def stop(self) -> None:
        """Stop the loop and the thread."""
        self.loop.call_soon_threadsafe(self.loop.stop)
        self._thread.join(timeout=5)
        self.loop.close()


@contextlib.contextmanager
def agent_thread() -> Iterator[AgentThread]:
    """A fresh agent loop for the duration of the block."""
    thread = AgentThread()
    try:
        yield thread
    finally:
        thread.stop()


# --- WebSocket shell -------------------------------------------------------------------------

Protocol = Literal["pcm", "twilio", "pipecat"]


@dataclass
class CallLog:
    """What the reference agent saw of one call."""

    brain: LoopbackBrain
    trial_id: str | None
    opening: list[dict[str, object]] = field(default_factory=list)
    closing: list[dict[str, object]] = field(default_factory=list)
    marks_back: list[str] = field(default_factory=list)
    caller_hung_up: bool = False


@dataclass
class WebSocketAgent:
    """Settings of the reference WebSocket agent, and the calls it took."""

    protocol: Protocol
    rate: int
    rhythm: Rhythm
    make_brain: Callable[[], LoopbackBrain]
    fail_after_replies: int | None = None
    """Close abnormally (1011) once this many replies have started."""
    calls: list[CallLog] = field(default_factory=list)


@contextlib.contextmanager
def serve_websocket(agent: WebSocketAgent) -> Iterator[str]:
    """Run the reference agent on an ephemeral 127.0.0.1 port; yield its ``ws://`` URL."""

    async def handler(connection: ServerConnection) -> None:
        request = connection.request
        trial_id = request.headers.get(TRIAL_ID_HEADER) if request is not None else None
        log = CallLog(agent.make_brain(), trial_id)
        agent.calls.append(log)
        await _converse(agent, log, connection)

    async def start() -> Server:
        return await serve(handler, "127.0.0.1", 0, max_size=None)

    async def stop(server: Server) -> None:
        server.close()
        await server.wait_closed()

    with agent_thread() as thread:
        server = thread.run(start())
        port = next(iter(server.sockets)).getsockname()[1]
        try:
            yield f"ws://127.0.0.1:{port}/"
        finally:
            thread.run(stop(server))


async def _converse(agent: WebSocketAgent, log: CallLog, connection: ServerConnection) -> None:
    brain = log.brain
    chunks_sent = 0

    async def send(pcm: Pcm16) -> None:
        nonlocal chunks_sent
        await connection.send(_encode(agent, pcm))
        if agent.protocol == "twilio" and agent.rhythm == "burst":
            chunks_sent += 1
            mark = {"event": "mark", "streamSid": "MZ", "mark": {"name": f"chunk-{chunks_sent}"}}
            await connection.send(json.dumps(mark))

    async def clear() -> None:
        match agent.protocol:
            case "pcm":
                await connection.send(json.dumps({"type": "clear"}))
            case "twilio":
                await connection.send(json.dumps({"event": "clear", "streamSid": "MZ"}))
            case "pipecat":
                await connection.send(_pipecat_interruption())

    async def listen(scope: anyio.CancelScope) -> None:
        with contextlib.suppress(ConnectionClosed):
            async for message in connection:
                _hear(agent, log, message)
        log.caller_hung_up = True
        scope.cancel()

    async def talk() -> None:
        if agent.fail_after_replies is not None:
            failing_after = agent.fail_after_replies
            await poll_until(lambda: len(brain.replies) >= failing_after)
            await sleep_until(brain.replies[-1].start_ms + 100)
            await connection.close(code=1011, reason="reference agent failure")
            return
        await speak(brain, Voice(agent.rhythm, agent.rate, send, clear))
        await connection.close()

    async with anyio.create_task_group() as group:
        group.start_soon(listen, group.cancel_scope)
        with contextlib.suppress(ConnectionClosed):
            await talk()


def _encode(agent: WebSocketAgent, pcm: Pcm16) -> str | bytes:
    match agent.protocol:
        case "pcm":
            return pcm.astype("<i2").tobytes()
        case "twilio":
            payload = base64.b64encode(mulaw_encode(pcm).astype(np.uint8).tobytes()).decode()
            return json.dumps({"event": "media", "streamSid": "MZ", "media": {"payload": payload}})
        case "pipecat":
            return pipecat_frame(pcm, agent.rate)


def _hear(agent: WebSocketAgent, log: CallLog, message: str | bytes) -> None:
    arrival = now_ms()
    if agent.protocol == "pipecat" and isinstance(message, bytes):
        samples, rate = pipecat_audio(message)
        log.brain.hear(samples, rate, arrival)
    elif isinstance(message, bytes):
        log.brain.hear(np.frombuffer(message, dtype="<i2").astype(np.int16), agent.rate, arrival)
    else:
        data = json.loads(message)
        kind = data.get("type") or data.get("event")
        if kind == "media":
            codes = np.frombuffer(base64.b64decode(data["media"]["payload"]), dtype=np.uint8)
            log.brain.hear(mulaw_decode(codes), 8000, arrival)
        elif kind == "mark":
            log.marks_back.append(data["mark"]["name"])
        elif kind == "stop":
            log.closing.append(data)
        else:
            log.opening.append(data)


def pipecat_audio(message: bytes) -> tuple[Pcm16, int]:
    """Samples and rate of a serialized Pipecat audio ``Frame``."""
    frame = frames_pb2.Frame.FromString(message)
    audio = frame.audio
    return np.frombuffer(audio.audio, dtype="<i2").astype(np.int16), int(audio.sample_rate)


def pipecat_frame(pcm: Pcm16, rate: int) -> bytes:
    """A serialized Pipecat audio ``Frame``."""
    audio = frames_pb2.AudioRawFrame(
        audio=pcm.astype("<i2").tobytes(), sample_rate=rate, num_channels=1
    )
    data: bytes = frames_pb2.Frame(audio=audio).SerializeToString()
    return data


def _pipecat_interruption() -> bytes:
    data: bytes = frames_pb2.Frame(interruption=frames_pb2.InterruptionFrame()).SerializeToString()
    return data
