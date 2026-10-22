"""The loopback agent's timing decisions, chunk by chunk, on a virtual clock."""

from collections.abc import Callable

import numpy as np
import pytest
from loopback_agent.call import GREETING_AFTER_MS, LoopbackCall, LoopbackSettings, duration_ms

from hecklr_voice.audio import FRAME_SAMPLES

SPEECH = (np.sin(np.arange(FRAME_SAMPLES) / 3) * 8000).astype(np.int16)
QUIET = np.zeros(FRAME_SAMPLES, dtype=np.int16)


def feed(call: LoopbackCall, frames: list[np.ndarray], start_ms: float = 0.0) -> list[bool]:
    """Deliver 20 ms frames on time from ``start_ms``; return what each ``hear`` returned."""
    return [call.hear(frame, start_ms + 20 * index) for index, frame in enumerate(frames)]


def make_call(settings: LoopbackSettings) -> LoopbackCall:
    return LoopbackCall(settings, reply_ms=3000.0, started_ms=0.0)


def test_sc4_reply_is_scheduled_exactly_the_delay_after_the_last_speech_sample() -> None:
    call = make_call(LoopbackSettings(reply_after_ms=600.0, endpoint_ms=200.0))
    scheduled = feed(call, [SPEECH] * 10 + [QUIET] * 15)
    # Speech ends at the last sample of frame 9 (200 ms); the endpoint fires 200 ms later.
    assert scheduled.index(True) == 20
    assert sum(scheduled) == 1
    (reply,) = call.replies
    assert reply.start_ms == pytest.approx(200.0 - 1 / 16 + 600.0, abs=0.1)
    assert reply.end_ms == reply.start_ms + 3000.0


def test_media_clock_ignores_late_arrivals() -> None:
    call = make_call(LoopbackSettings(reply_after_ms=600.0))
    frames = [SPEECH] * 5 + [QUIET] * 15
    # Each frame after the first arrives up to 15 ms late: the timeline must not move.
    for index, frame in enumerate(frames):
        call.hear(frame, 20 * index + (15.0 if index % 2 else 0.0))
    (reply,) = call.replies
    assert reply.start_ms == pytest.approx(100.0 - 1 / 16 + 600.0, abs=0.1)


def test_sc4_barge_in_cuts_the_reply_the_reaction_after_the_caller_onset() -> None:
    call = make_call(LoopbackSettings(reply_after_ms=300.0, barge_in_reaction_ms=150.0))
    feed(call, [SPEECH] * 5 + [QUIET] * 40)  # reply planned at ~400 ms, playing at 900 ms
    reply = call.replies[0]
    onset = 45 * 20.0
    call.hear(SPEECH, onset)
    assert reply.cut_ms == pytest.approx(onset + 150.0, abs=0.1)
    assert reply.end_ms == reply.cut_ms


def test_the_next_reply_waits_for_the_current_one_to_end() -> None:
    call = make_call(LoopbackSettings(reply_after_ms=300.0, barge_in_reaction_ms=None))
    feed(call, [SPEECH] * 5 + [QUIET] * 40 + [SPEECH] * 5 + [QUIET] * 15)
    first, second = call.replies
    assert first.cut_ms is None
    assert second.start_ms == pytest.approx(first.end_ms)


def test_speech_outside_a_reply_cuts_nothing() -> None:
    call = make_call(LoopbackSettings(reply_after_ms=300.0))
    feed(call, [SPEECH] * 3)
    assert call.replies == []


def test_greeting_plays_shortly_after_the_call_starts() -> None:
    call = LoopbackCall(LoopbackSettings(greeting=True), reply_ms=1000.0, started_ms=50.0)
    (greeting,) = call.replies
    assert greeting.start_ms == 50.0 + GREETING_AFTER_MS


@pytest.mark.parametrize(
    ("make", "message"),
    [
        (lambda: LoopbackSettings(endpoint_ms=0.0), "endpoint_ms must be positive"),
        (lambda: LoopbackSettings(reply_after_ms=210.0, endpoint_ms=200.0), "at least one frame"),
        (lambda: LoopbackSettings(barge_in_reaction_ms=0.0), "must be positive"),
    ],
)
def test_invalid_settings_are_refused(make: Callable[[], LoopbackSettings], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        make()


def test_duration_of_audio() -> None:
    assert duration_ms(np.zeros(16_000, dtype=np.int16)) == 1000.0
