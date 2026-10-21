"""The playout timeline: real-time release, placement by arrival, clear, marks, end of stream."""

import anyio
import numpy as np
import pytest

from hecklr_adapters import AgentError
from hecklr_adapters.voice.audio import FRAME_SAMPLES
from hecklr_adapters.voice.playout import Playout, Receiver
from hecklr_schemas import TimedFrame


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


def audio(ms: float, value: int = 1000) -> np.ndarray:
    return np.full(round(ms * 16), value, dtype=np.int16)


def drain(playout: Playout) -> list[TimedFrame]:
    frames = []
    while (frame := playout.pop_frame()) is not None:
        frames.append(frame)
    return frames


def test_audio_on_a_quiet_line_plays_from_its_arrival() -> None:
    clock = Clock()
    playout = Playout(clock)
    clock.now += 500.25
    playout.write(audio(40))
    first = playout.pop_frame()
    assert first is not None
    assert first.sample_index == 8004  # 500.25 ms on the 16 kHz media clock
    assert first.sample_rate == 16_000
    assert len(first.pcm) == FRAME_SAMPLES * 2
    assert playout.pop_frame() is None  # the second frame is not due yet
    assert playout.wait_s() == pytest.approx(0.02)
    clock.now += 20
    second = playout.pop_frame()
    assert second is not None
    assert second.sample_index == 8004 + FRAME_SAMPLES


def test_a_burst_is_released_in_real_time() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(1000))
    released = []
    for _ in range(100):
        released += drain(playout)
        clock.now += 5
    assert len(released) == 25  # 500 ms of the 1 s burst have been heard after 500 ms
    indices = [frame.sample_index for frame in released]
    assert indices == list(range(0, 25 * FRAME_SAMPLES, FRAME_SAMPLES))


def test_audio_arriving_while_playing_queues_behind_it() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(40))
    clock.now += 10
    playout.write(audio(40, value=2000))  # contiguous: it plays after the first 40 ms
    frames = []
    for _ in range(20):
        frames += drain(playout)
        clock.now += 10
    assert [frame.sample_index for frame in frames] == [0, 320, 640, 960]
    assert np.frombuffer(frames[2].pcm, "<i2")[0] == 2000


def test_a_late_packet_joins_its_run_but_a_gap_starts_a_new_one() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(20))
    assert drain(playout)[0].sample_index == 0
    clock.now += 50  # 30 ms late for its slot at 20 ms: jitter, still the same run
    playout.write(audio(20))
    assert drain(playout)[0].sample_index == FRAME_SAMPLES
    clock.now += 200  # the line went quiet: a new run at the arrival time
    playout.write(audio(20))
    assert drain(playout)[0].sample_index == 250 * 16


def test_a_partial_frame_waits_one_frame_for_the_rest() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(10))
    assert playout.pop_frame() is None
    assert playout.wait_s() == pytest.approx(0.02)
    clock.now += 5
    playout.write(audio(10, value=2000))  # completes the frame
    frame = playout.pop_frame()
    assert frame is not None
    assert len(frame.pcm) == FRAME_SAMPLES * 2
    playout.write(audio(5))
    clock.now += 40
    short = playout.pop_frame()
    assert short is not None
    assert len(short.pcm) == 5 * 16 * 2


def test_clear_drops_unplayed_audio_and_returns_pending_marks() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(1000))
    assert playout.mark("reply-1") == []
    drain(playout)
    cleared = playout.clear()
    assert [mark.name for mark in cleared] == ["reply-1"]
    assert playout.idle
    assert playout.wait_s() is None
    assert playout.mark("now") != []  # nothing queued: a mark is reported at once


def test_marks_are_reported_once_their_audio_has_played() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(40))
    playout.mark("end")
    drain(playout)
    assert playout.played_marks() == []
    clock.now += 20
    drain(playout)
    assert [mark.name for mark in playout.played_marks()] == ["end"]


def test_end_releases_a_partial_frame_without_waiting() -> None:
    clock = Clock()
    playout = Playout(clock)
    playout.write(audio(30))
    drain(playout)
    clock.now += 20
    assert playout.pop_frame() is None
    playout.end()
    assert playout.wait_s() == 0.0
    last = playout.pop_frame()
    assert last is not None
    assert len(last.pcm) == 10 * 16 * 2


@pytest.mark.anyio
async def test_receiver_reads_in_the_background_and_plays_out_after_the_end() -> None:
    playout = Playout()
    sent = [audio(60)]

    async def read() -> bool:
        if not sent:
            return False
        playout.write(sent.pop())
        return True

    receiver = Receiver(playout, read)
    receiver.start()
    frames = [frame async for frame in receiver.frames()]
    await receiver.stop()
    start = frames[0].sample_index
    assert [frame.sample_index for frame in frames] == [start + k * FRAME_SAMPLES for k in range(3)]


@pytest.mark.anyio
async def test_receiver_raises_a_transport_failure_after_the_audio_before_it() -> None:
    playout = Playout()
    reads = 0

    async def read() -> bool:
        nonlocal reads
        reads += 1
        if reads > 1:
            raise AgentError("connection reset")
        playout.write(audio(20))
        return True

    receiver = Receiver(playout, read)
    receiver.start()
    heard: list[TimedFrame] = []

    async def listen() -> None:
        async for frame in receiver.frames():
            heard.append(frame)

    with pytest.raises(AgentError, match="reset"):
        await listen()
    assert len(heard) == 1
    await receiver.stop()


@pytest.mark.anyio
async def test_stopping_a_receiver_cancels_its_read() -> None:
    async def read() -> bool:
        await anyio.sleep(10)
        return True

    receiver = Receiver(Playout(), read)
    receiver.start()
    await receiver.stop()
    await receiver.stop()
