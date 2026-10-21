"""Clocks, frame pacing, the caller stream, agent segmentation, recordings and turn-taking."""

import wave
from pathlib import Path

import numpy as np
import pytest

from hecklr_schemas import Behaviors
from hecklr_voice.audio import FRAME_SAMPLES, SAMPLE_RATE
from hecklr_voice.recording import CallRecorder, RecordingPaths, Track
from hecklr_voice.streams import AgentSpeech, CallerStream, Segment
from hecklr_voice.timing import FramePacer, MediaClock, TrialTime
from hecklr_voice.turntaking import (
    BACKCHANNEL_WINDOW_MS,
    BARGE_IN_WINDOW_MS,
    FILLERS,
    RE_ENGAGEMENT_MS,
    TurnTakingPolicy,
)


class VirtualClock:
    """Simulated time: each sleep overshoots by a seeded 0-3 ms, like a real OS timer."""

    def __init__(self, seed: int) -> None:
        self.now = 0.0
        self._generator = np.random.default_rng(seed)

    def now_ms(self) -> float:
        return self.now

    async def sleep_until(self, deadline_ms: float) -> None:
        if deadline_ms > self.now:
            self.now = deadline_ms + float(self._generator.uniform(0.0, 3.0))


@pytest.mark.anyio
async def test_sc4_ten_minute_call_has_no_cumulative_clock_drift() -> None:
    """30,000 frames on a jittery clock: send time never drifts from frame-count time."""
    clock = VirtualClock(seed=1)
    pacer = FramePacer(0.0, clock.now_ms, clock.sleep_until)
    frames = 10 * 60 * 1000 // 20
    worst_offset = 0.0
    for index in range(frames):
        await pacer.wait(index)
        # Sending takes time too; it must not push later frames back.
        clock.now += 0.5
        worst_offset = max(worst_offset, abs(clock.now - pacer.due_ms(index)))
    # The last frame left when frame counts say it should: 599,980 ms after the first.
    assert abs(clock.now - (frames - 1) * 20) < 5.0
    assert worst_offset < 5.0
    assert pacer.underruns == 0
    assert pacer.max_lateness_ms <= 3.0


@pytest.mark.anyio
async def test_pacer_counts_frames_sent_a_whole_period_late() -> None:
    clock = VirtualClock(seed=2)
    pacer = FramePacer(0.0, clock.now_ms, clock.sleep_until)
    await pacer.wait(0)
    clock.now += 70.0  # a 70 ms stall
    assert await pacer.wait(1) >= 50.0
    assert await pacer.wait(2) >= 30.0  # still behind: sent at once to catch up
    assert await pacer.wait(3) < 20.0
    assert await pacer.wait(4) <= 3.0  # back on the grid: only timer overshoot remains
    assert pacer.underruns == 2


@pytest.mark.anyio
async def test_trial_time_follows_the_sink_origin_and_sleeps() -> None:
    ticks = iter([0, 1_500_000, 3_000_000])
    clock = TrialTime(1000, now_ns=lambda: next(ticks))
    assert clock.now_ms() == pytest.approx(1001.5)
    assert clock.now_ms() == pytest.approx(1003.0)
    real = TrialTime(0)
    await real.sleep_until(real.now_ms() + 5)
    await real.sleep_until(0)
    assert real.now_ms() >= 5


def test_media_clock_uses_the_earliest_arrival() -> None:
    clock = MediaClock()
    with pytest.raises(RuntimeError, match="origin"):
        clock.to_ms(0)
    clock.observe(0, 103.0)
    clock.observe(320, 121.5)  # 1.5 ms earlier than the first frame implies
    clock.observe(640, 150.0)  # late: ignored
    assert clock.to_ms(0) == pytest.approx(101.5)
    assert clock.to_ms(16_000) == pytest.approx(1101.5)


def test_caller_stream_plays_clips_in_order_then_idles() -> None:
    idle = np.full(2 * FRAME_SAMPLES, 7, dtype=np.int16)
    stream = CallerStream(idle)
    assert stream.take(0)[0] == 7
    first = stream.play(np.full(2 * FRAME_SAMPLES, 1, dtype=np.int16))
    second = stream.play(np.full(FRAME_SAMPLES, 2, dtype=np.int16))
    assert (first.start_frame, second.start_frame) == (1, 3)
    busy_before = stream.busy
    assert [stream.take(index)[0] for index in range(1, 5)] == [1, 1, 2, 7]
    assert first.done.is_set()
    assert second.done.is_set()
    assert (busy_before, stream.busy) == (True, False)
    with pytest.raises(ValueError, match="whole number"):
        stream.play(np.zeros(10, dtype=np.int16))
    with pytest.raises(ValueError, match="idle line"):
        CallerStream(np.zeros(0, dtype=np.int16))


def _frame(level: float, start: int = 0, end: int = FRAME_SAMPLES) -> np.ndarray:
    frame = np.zeros(FRAME_SAMPLES, dtype=np.int16)
    frame[start:end] = int(level * 32767)
    return frame


def test_agent_speech_segments_with_sample_accurate_edges_and_hangover() -> None:
    opened: list[Segment] = []
    closed: list[Segment] = []
    speech = AgentSpeech(100, opened.append, closed.append)
    speech.frame(0.0, 0, _frame(0.0), speech=False)
    speech.frame(20.0, 320, _frame(0.3, start=160), speech=True)
    assert opened[0].start_ms == pytest.approx(30.0)
    assert opened[0].start_sample == 480
    speech.frame(40.0, 640, _frame(0.3, end=80), speech=True)
    for index in range(3, 10):
        speech.frame(index * 20.0, index * 320, _frame(0.0), speech=False)
    assert closed[0].last_ms == pytest.approx(45.0)
    assert closed[0].closed
    assert speech.last_end_ms == pytest.approx(45.0)
    assert speech.open is None
    assert not speech.speaking_at(200.0, 40.0)


def test_agent_speech_closes_when_frames_stop_arriving() -> None:
    closed: list[Segment] = []
    speech = AgentSpeech(100, lambda segment: None, closed.append)
    speech.frame(0.0, 0, _frame(0.3), speech=True)
    assert speech.speaking_at(30.0, 40.0)
    speech.quiet_until(50.0)
    assert not closed
    speech.quiet_until(130.0)
    assert closed[0].last_ms == pytest.approx(20.0)
    speech.close()  # nothing open: no-op
    assert len(closed) == 1


def test_track_grows_and_reads_silence_beyond_its_end() -> None:
    track = Track(capacity=4)
    track.write(2, np.array([1, 2, 3], dtype=np.int16))
    track.write(10, np.array([9], dtype=np.int16))
    assert track.length == 11
    assert track.read(0, 6).tolist() == [0, 0, 1, 2, 3, 0]
    assert track.read(10, 13).tolist() == [9, 0, 0]
    assert track.samples().tolist()[-1] == 9
    with pytest.raises(ValueError, match=">= 0"):
        track.write(-1, np.zeros(1, dtype=np.int16))


def test_recorder_writes_caller_agent_and_stereo_mix(tmp_path: Path) -> None:
    recorder = CallRecorder()
    recorder.caller.write(0, np.full(SAMPLE_RATE, 100, dtype=np.int16))
    recorder.agent.write(SAMPLE_RATE // 2, np.full(SAMPLE_RATE, -100, dtype=np.int16))
    paths = RecordingPaths("audio/trial/attempt")
    recorder.write(tmp_path, paths)
    with wave.open(str(tmp_path / paths.mix)) as mix:
        assert (mix.getnchannels(), mix.getframerate()) == (2, SAMPLE_RATE)
        stereo = np.frombuffer(mix.readframes(mix.getnframes()), dtype="<i2").reshape(-1, 2)
    assert len(stereo) == SAMPLE_RATE * 3 // 2
    assert stereo[0].tolist() == [100, 0]
    assert stereo[-1].tolist() == [0, -100]
    for path in (paths.caller, paths.agent):
        with wave.open(str(tmp_path / path)) as mono:
            assert mono.getnchannels() == 1
            assert mono.getnframes() == SAMPLE_RATE * 3 // 2
    with pytest.raises(FileExistsError):
        recorder.write(tmp_path, paths)  # append-only: never overwrite a recording


def test_turn_taking_statistics_match_persona_params() -> None:
    behaviors = Behaviors(
        interrupt_prob=0.25,
        backchannel_prob=0.4,
        silence_prob=0.1,
        mid_utterance_pause_s=(0.4, 1.2),
        response_gap_ms=(200, 900),
        fillers=True,
    )
    trials = 4000
    policies = [TurnTakingPolicy(behaviors, seed) for seed in range(trials // 100)]
    replies = [policy.plan_reply(ordinal) for policy in policies for ordinal in range(100)]
    barge = [plan.barge_in_after_ms for plan in replies if plan.barge_in_after_ms is not None]
    back = [plan.backchannel_after_ms for plan in replies if plan.backchannel_after_ms is not None]
    assert len(barge) / trials == pytest.approx(0.25, abs=0.03)
    # A backchannel is drawn only when the caller did not barge in.
    assert len(back) / trials == pytest.approx(0.75 * 0.4, abs=0.03)
    assert min(barge) >= BARGE_IN_WINDOW_MS[0]
    assert max(barge) <= BARGE_IN_WINDOW_MS[1]
    assert min(back) >= BACKCHANNEL_WINDOW_MS[0]
    assert max(back) <= BACKCHANNEL_WINDOW_MS[1]
    gaps = [policy.response_gap_ms(ordinal) for policy in policies for ordinal in range(100)]
    assert min(gaps) >= 200
    assert max(gaps) <= 900
    assert np.mean(gaps) == pytest.approx(550, abs=15)
    silences = [policy.silence_ms(ordinal) for policy in policies for ordinal in range(100)]
    drawn = [value for value in silences if value is not None]
    assert len(drawn) / trials == pytest.approx(0.1, abs=0.02)
    assert min(drawn) >= RE_ENGAGEMENT_MS[0]
    assert max(drawn) <= RE_ENGAGEMENT_MS[1]
    text = "I would like to move my boiler visit to next week please"
    plans = [policy.plan_utterance(ordinal, text) for policy in policies for ordinal in range(100)]
    paused = [plan for plan in plans if plan.pause_ms is not None]
    assert len(paused) / trials == pytest.approx(0.5, abs=0.03)
    assert all(400 <= plan.pause_ms <= 1200 for plan in paused if plan.pause_ms is not None)
    assert all(plan.text == text for plan in plans)
    with_filler = [plan for plan in plans if plan.filler is not None]
    assert len(with_filler) / trials == pytest.approx(0.3, abs=0.03)
    assert {plan.filler for plan in with_filler} <= set(FILLERS)


def test_turn_taking_is_seeded_and_respects_disabled_behaviors() -> None:
    quiet = TurnTakingPolicy(Behaviors(), seed=9)
    assert quiet.plan_reply(0).barge_in_after_ms is None
    assert quiet.plan_reply(0).backchannel_after_ms is None
    assert quiet.silence_ms(0) is None
    plan = quiet.plan_utterance(0, "please move my visit to next week")
    assert (plan.parts, plan.filler, plan.pause_ms) == (
        ("please move my visit to next week",),
        None,
        None,
    )
    busy = Behaviors(interrupt_prob=1.0, mid_utterance_pause_s=(0.5, 0.5))
    assert TurnTakingPolicy(busy, 1).plan_reply(3) == TurnTakingPolicy(busy, 1).plan_reply(3)
    assert TurnTakingPolicy(busy, 1).plan_utterance(0, "too short").pause_ms is None
    assert TurnTakingPolicy(busy, 1).interjection(0) == TurnTakingPolicy(busy, 1).interjection(0)
    assert TurnTakingPolicy(busy, 1).backchannel(2) in {"mm-hm", "okay", "right", "yeah"}
