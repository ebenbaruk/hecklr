"""The caller's earpiece: plays the agent's audio out in real time, as a phone would.

Agents send audio in whatever rhythm their transport allows: paced 20 ms packets (WebRTC,
Pipecat's WebSocket output) or seconds of TTS in one burst (Twilio-style media streams, which
the phone network buffers and plays). The harness needs neither rhythm but the one a customer
hears, so every adapter feeds received audio into a :class:`Playout` and the driver reads the
paced :class:`TimedFrame` stream it produces.

The timeline is the receive stream's media clock in 16 kHz samples, started when the call
connects. Audio that arrives while the line is quiet starts playing at its arrival time; audio
that arrives while earlier audio is still playing queues behind it. Each 20 ms frame is released
when its first sample is due, so ``sample_index`` maps to time exactly as the driver's media
clock assumes (ADR 0009a). Silence between replies is skipped in the index, never sent as
frames. ``clear()`` drops everything not yet played, which is how a transport-level barge-in
(Twilio ``clear``, Pipecat's interruption frame) stops the agent mid-sentence.
"""

import asyncio
import contextlib
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass
from typing import Final

import anyio
import numpy as np

from hecklr_adapters.voice.audio import FRAME_MS, FRAME_SAMPLES, SAMPLE_RATE, SAMPLES_PER_MS, Pcm16
from hecklr_schemas import TimedFrame

REJOIN_MS: Final = 2 * FRAME_MS
"""Audio this late for its place in the queue starts a new run at its arrival instead.

A packet a little late (network jitter) still plays right after the previous one; one that
missed its slot by more than two frames means the line went quiet in between, as a real jitter
buffer would conclude.
"""


def monotonic_ms() -> float:
    """The adapter's clock: monotonic milliseconds with sub-millisecond resolution."""
    return time.monotonic_ns() / 1e6


@dataclass(frozen=True, slots=True)
class Mark:
    """A named position on the timeline (Twilio ``mark``), reported once played or cleared."""

    name: str
    position: int


class Playout:
    """Real-time playout of received agent audio as 16 kHz :class:`TimedFrame` s."""

    def __init__(self, now_ms: Callable[[], float] = monotonic_ms) -> None:
        self._now_ms = now_ms
        self._origin_ms = now_ms()
        self._pending = np.zeros(0, dtype=np.int16)
        self._played = 0
        """Timeline position of the first sample not yet released; ``_pending`` starts here."""
        self._marks: list[Mark] = []
        self.ended = False

    @property
    def idle(self) -> bool:
        """Nothing is queued to play."""
        return len(self._pending) == 0

    def write(self, samples: Pcm16) -> None:
        """Queue received 16 kHz audio, stamped with the current time."""
        if len(samples) == 0:
            return
        now = self._position(self._now_ms())
        if self.idle and self._played < now - REJOIN_MS * SAMPLES_PER_MS:
            self._played = now
        self._pending = np.concatenate([self._pending, samples])

    def clear(self) -> list[Mark]:
        """Drop all audio not yet played; return the marks that will now never play."""
        self._pending = self._pending[:0]
        cleared, self._marks = self._marks, []
        return cleared

    def mark(self, name: str) -> list[Mark]:
        """Place a mark after the queued audio; returned at once if nothing is queued."""
        self._marks.append(Mark(name, self._played + len(self._pending)))
        return self.played_marks()

    def played_marks(self) -> list[Mark]:
        """Marks whose audio has been released, removed from the queue."""
        done = [mark for mark in self._marks if mark.position <= self._played]
        self._marks = [mark for mark in self._marks if mark.position > self._played]
        return done

    def end(self) -> None:
        """The agent stopped sending: release the rest without waiting for more audio."""
        self.ended = True

    def pop_frame(self) -> TimedFrame | None:
        """The next frame, if it is due now."""
        if self.idle:
            return None
        now_ms = self._now_ms()
        due_ms = self._time(self._played)
        if now_ms < due_ms:
            return None
        if len(self._pending) < FRAME_SAMPLES and not self.ended and now_ms < due_ms + FRAME_MS:
            return None  # a partial frame waits up to one frame period for the rest
        frame, self._pending = self._pending[:FRAME_SAMPLES], self._pending[FRAME_SAMPLES:]
        start = self._played
        self._played += len(frame)
        return TimedFrame(
            pcm=frame.astype("<i2").tobytes(),
            sample_rate=SAMPLE_RATE,
            sample_index=start,
            t_ms=round(now_ms - self._origin_ms),
        )

    def wait_s(self) -> float | None:
        """Seconds until :meth:`pop_frame` may return a frame; ``None`` if only new audio can."""
        if self.idle:
            return None
        release_ms = self._time(self._played)
        if len(self._pending) < FRAME_SAMPLES and not self.ended:
            release_ms += FRAME_MS
        return max(0.0, (release_ms - self._now_ms()) / 1000)

    def _position(self, at_ms: float) -> int:
        return round((at_ms - self._origin_ms) * SAMPLES_PER_MS)

    def _time(self, position: int) -> float:
        return self._origin_ms + position / SAMPLES_PER_MS


Read = Callable[[], Awaitable[bool]]
"""Wait for one inbound item from the transport and handle it; ``False`` once the stream ended."""


class Receiver:
    """Reads the agent's side of a call in the background and serves the playout's frames.

    Audio must be stamped when it arrives, not when the driver gets round to reading it: the
    driver does other work between frames, and a transport that queues frames meanwhile (a
    socket buffer, LiveKit's stream queue) would otherwise hand over a backlog that looks like
    late audio. So a task reads from the moment the call connects, and :meth:`frames` only
    paces what the playout holds. The task is a plain asyncio task because it outlives the
    ``connect`` call that starts it (ADR 0006a: adapters run on asyncio).
    """

    def __init__(self, playout: Playout, read: Read) -> None:
        self.playout = playout
        self._read = read
        self._task: asyncio.Task[None] | None = None
        self._error: BaseException | None = None
        self._ended = False
        self._wake = asyncio.Event()

    def start(self) -> None:
        """Start reading."""
        self._task = asyncio.get_running_loop().create_task(self._run())

    async def stop(self) -> None:
        """Stop reading (idempotent)."""
        if self._task is not None and not self._task.done():
            self._task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._task

    async def frames(self) -> AsyncIterator[TimedFrame]:
        """Frames as they come due, until the agent's stream has ended and played out.

        Raises:
            AgentError: the transport failed (re-raised from the reading task), after the
                audio received before the failure has been played.
        """
        while True:
            frame = self.playout.pop_frame()
            if frame is not None:
                yield frame
                continue
            if self._ended:
                self.playout.end()
                if self.playout.idle:
                    if self._error is not None:
                        raise self._error
                    return
            self._wake.clear()
            with anyio.move_on_after(_or_forever(self.playout.wait_s())):
                await self._wake.wait()

    async def _run(self) -> None:
        try:
            while await self._read():
                self._wake.set()
        except Exception as exc:  # noqa: BLE001 -- handed to the consumer of frames(), not lost
            self._error = exc
        finally:
            self._ended = True
            self._wake.set()


def _or_forever(seconds: float | None) -> float:
    return float("inf") if seconds is None else seconds
