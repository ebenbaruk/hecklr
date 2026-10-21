"""Clocks for a call: sub-millisecond trial time, drift-free frame pacing, receive media time.

Every voice timing comes from frame counts (spec, "Metrics"). The caller's audio is placed on
the send stream by frame index (frame ``k`` starts at ``origin + 20 k`` ms, however late it
actually left); the agent's audio is placed by ``TimedFrame.sample_index`` through
:class:`MediaClock`. Wall-clock wake-up jitter therefore never accumulates into a timing.
"""

import time
from collections.abc import Awaitable, Callable

import anyio

from hecklr_voice.audio import FRAME_MS, SAMPLES_PER_MS


class TrialTime:
    """The trial clock in fractional milliseconds, aligned to the sink's integer clock.

    ``EventSink.now_ms()`` has whole-millisecond resolution; frame scheduling needs better, so
    this reads the monotonic clock directly from an origin taken from the sink.
    """

    def __init__(self, sink_now_ms: int, *, now_ns: Callable[[], int] = time.monotonic_ns) -> None:
        self._now_ns = now_ns
        self._base_ms = sink_now_ms
        self._base_ns = now_ns()

    def now_ms(self) -> float:
        """Milliseconds since the trial started."""
        return self._base_ms + (self._now_ns() - self._base_ns) / 1e6

    async def sleep_until(self, deadline_ms: float) -> None:
        """Sleep until the trial clock reaches ``deadline_ms`` (returns at once if past)."""
        remaining = deadline_ms - self.now_ms()
        if remaining > 0:
            await anyio.sleep(remaining / 1000)


class FramePacer:
    """Schedules 20 ms frames from their index, so lateness never turns into drift.

    Frame ``k`` is due at ``origin_ms + 20 k`` (plus any deliberate jitter). A frame that wakes
    up late is sent at once and the next one is still due on the original grid, so the stream
    catches up instead of slipping. A frame sent a full frame period late is an underrun: the
    receiver's buffer would have run dry.
    """

    def __init__(
        self,
        origin_ms: float,
        now_ms: Callable[[], float],
        sleep_until: Callable[[float], Awaitable[None]],
    ) -> None:
        self.origin_ms = origin_ms
        self._now_ms = now_ms
        self._sleep_until = sleep_until
        self.underruns = 0
        self.max_lateness_ms = 0.0

    def due_ms(self, index: int) -> float:
        """When frame ``index`` is due, before any deliberate jitter."""
        return self.origin_ms + index * FRAME_MS

    async def wait(self, index: int, extra_delay_ms: float = 0.0) -> float:
        """Sleep until frame ``index`` is due and return how late the wake-up was (ms)."""
        due = self.due_ms(index) + extra_delay_ms
        await self._sleep_until(due)
        lateness = max(0.0, self._now_ms() - due)
        self.max_lateness_ms = max(self.max_lateness_ms, lateness)
        if lateness >= FRAME_MS:
            self.underruns += 1
        return lateness


class MediaClock:
    """Maps positions on a receive stream (samples) to trial time.

    Frames are paced in real time by the sender, so ``arrival - position`` is the stream's
    origin plus network and scheduling delay. The smallest value seen is the best estimate of
    the origin (earliest-arrival alignment, as RTP receivers do), which removes arrival jitter
    from every timing derived from sample positions.
    """

    def __init__(self) -> None:
        self._offset_ms: float | None = None

    def observe(self, sample_index: int, arrival_ms: float) -> None:
        """Record that the sample at ``sample_index`` arrived at ``arrival_ms``."""
        offset = arrival_ms - sample_index / SAMPLES_PER_MS
        if self._offset_ms is None or offset < self._offset_ms:
            self._offset_ms = offset

    def to_ms(self, sample_index: float) -> float:
        """Trial time of a stream position; needs at least one observation."""
        if self._offset_ms is None:
            raise RuntimeError("no frame observed yet: the media clock has no origin")
        return self._offset_ms + sample_index / SAMPLES_PER_MS
