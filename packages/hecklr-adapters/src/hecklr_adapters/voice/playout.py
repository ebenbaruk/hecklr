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


Receive = Callable[[float | None], Awaitable[bool]]
"""Wait up to the given seconds (``None``: indefinitely) for one inbound item and handle it.

Returns ``False`` once the agent's stream has ended. Timing out is not an error: it returns
``True`` so the playout can release a frame that has come due.
"""


async def paced_frames(playout: Playout, receive: Receive) -> AsyncIterator[TimedFrame]:
    """Drive ``receive`` and yield the playout's frames as they come due, to the stream's end.

    One loop does both, so an adapter needs no background task: it reads the transport only
    while the driver is consuming frames, and the playout decides when each frame is heard.
    """
    while True:
        frame = playout.pop_frame()
        if frame is not None:
            yield frame
            continue
        if playout.ended:
            if playout.idle:
                return
            await anyio.sleep(playout.wait_s() or 0.0)
            continue
        if not await receive(playout.wait_s()):
            playout.end()
