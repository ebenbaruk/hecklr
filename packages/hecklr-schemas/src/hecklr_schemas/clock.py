"""The harness clock: every event time is integer milliseconds on a monotonic clock."""

import time
from typing import Protocol, runtime_checkable


@runtime_checkable
class Clock(Protocol):
    """Source of harness time in integer milliseconds.

    Monotonic, never wall-clock: wall time can jump (NTP, DST) and would corrupt latency numbers.
    Voice code derives sub-millisecond positions from frame counts and converts with this clock.
    """

    def now_ms(self) -> int:
        """Return milliseconds elapsed since the clock's origin."""
        ...


class MonotonicClock:
    """Real harness clock; ``now_ms()`` is 0 when the clock is created."""

    def __init__(self) -> None:
        self._origin_ns = time.monotonic_ns()

    def now_ms(self) -> int:
        """Return whole milliseconds since construction."""
        return (time.monotonic_ns() - self._origin_ns) // 1_000_000


class FakeClock:
    """Manually advanced clock for deterministic tests."""

    def __init__(self, start_ms: int = 0) -> None:
        if start_ms < 0:
            raise ValueError(f"start_ms must be >= 0, got {start_ms}")
        self._now_ms = start_ms

    def now_ms(self) -> int:
        """Return the current fake time."""
        return self._now_ms

    def advance(self, ms: int) -> int:
        """Move time forward by ``ms`` (never backwards) and return the new time."""
        if ms < 0:
            raise ValueError(f"a monotonic clock cannot go backwards (advance by {ms} ms)")
        self._now_ms += ms
        return self._now_ms
