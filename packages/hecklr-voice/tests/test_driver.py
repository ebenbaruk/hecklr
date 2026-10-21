"""VoiceDriver end to end against the in-process loopback agent: SC4, events, limits, failures.

Calls run in real time, so tests keep replies short and run independent calls concurrently.
"""

import wave
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from typing import Any

import anyio
import numpy as np
import pytest

from hecklr_adapters import AgentError, AgentTimeoutError
from hecklr_grade import measure
from hecklr_schemas import (
    AudioLink,
    AudioSpanPayload,
    ChannelDriver,
    DriverResult,
    EndReason,
    SessionContext,
    SimEnd,
    TimedFrame,
    TrialBrief,
    TrialId,
    UtterancePayload,
    new_id,
)
from hecklr_schemas.common import Actor
from hecklr_schemas.scenario import VoiceMetric
from hecklr_voice.asr import FakeASR
from hecklr_voice.audio import SAMPLE_RATE, SPEECH_AMPLITUDE
from hecklr_voice.call import CallStats, VoiceSettings
from hecklr_voice.driver import VoiceDriver
from hecklr_voice.testing import (
    LoopbackVoiceTarget,
    RecordingSink,
    ScriptedCaller,
    synthetic_reply,
    voice_trial,
)


def context(timeout_s: float = 10.0) -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=3, timeout_s=timeout_s)


# Real-time calls: keep pytest's own garbage collections from pausing the loop mid-call.
pytestmark = [pytest.mark.anyio, pytest.mark.usefixtures("frozen_heap")]

SHORT_REPLY_TEXT = "sure I can help with that"
LONG_REPLY_TEXT = (
    "sure I can help with that let me look up your appointment and find a better slot for "
    "you next week in the morning while we are on the line"
)
LINES = ["Hi, I need to move my boiler visit.", "Next Tuesday morning works.", "Thanks!"]
FAST = VoiceSettings(greeting_wait_ms=200)
TOLERANCE_MS = 20
PROCESSING_ALLOWANCE_MS = 2_000


async def _call(
    target: Any,
    trial: TrialBrief,
    lines: Sequence[str] = LINES,
    *,
    settings: VoiceSettings = FAST,
    ctx: SessionContext | None = None,
    run_dir: Path | None = None,
    end: SimEnd | None = None,
) -> tuple[DriverResult, RecordingSink, list[CallStats]]:
    sink = RecordingSink()
    stats: list[CallStats] = []
    driver = VoiceDriver(
        target, asr=FakeASR(), settings=settings, run_dir=run_dir, on_call_end=stats.append
    )
    result = await driver.run(ctx or context(), trial, ScriptedCaller(lines, end), sink)
    return result, sink, stats


def _loopback(**options: Any) -> LoopbackVoiceTarget:
    text = options.pop("text", SHORT_REPLY_TEXT)
    return LoopbackVoiceTarget(reply=synthetic_reply(text), **options)


async def test_ws9_voice_driver_satisfies_the_channel_driver_protocol() -> None:
    driver = VoiceDriver(_loopback(), asr=FakeASR())
    assert isinstance(driver, ChannelDriver)
    assert driver.channel.value == "voice"


async def test_sc4_response_latency_within_20ms_of_injected_delay() -> None:
    results: dict[int, RecordingSink] = {}

    async def measure_delay(delay_ms: int) -> None:
        _, sink, _ = await _call(_loopback(reply_after_ms=delay_ms), voice_trial())
        results[delay_ms] = sink

    async with anyio.create_task_group() as group:
        for delay_ms in (300, 600, 900):
            group.start_soon(measure_delay, delay_ms)
    for delay_ms, sink in results.items():
        latencies = [p.latency_ms for _, p in sink.utterances("agent") if p.latency_ms is not None]
        assert len(latencies) == len(LINES)
        assert all(abs(latency - delay_ms) <= TOLERANCE_MS for latency in latencies), latencies
        p50 = measure(VoiceMetric.RESPONSE_LATENCY, sink.events, duration_ms=1, q="p50")
        assert not isinstance(p50, str)
        assert abs(p50.value - delay_ms) <= TOLERANCE_MS


async def test_sc4_barge_in_reaction_within_20ms_of_injected_reaction() -> None:
    results: dict[int, RecordingSink] = {}

    async def measure_reaction(reaction_ms: int) -> None:
        target = _loopback(
            text=LONG_REPLY_TEXT, reply_after_ms=300, barge_in_reaction_ms=reaction_ms
        )
        trial = voice_trial(behaviors={"interrupt_prob": 1.0}, max_turns=8)
        _, sink, _ = await _call(target, trial, LINES[:2])
        results[reaction_ms] = sink

    async with anyio.create_task_group() as group:
        for reaction_ms in (150, 300):
            group.start_soon(measure_reaction, reaction_ms)
    for reaction_ms, sink in results.items():
        barge_ins = [p for _, p in sink.interrupts() if p.style == "barge_in"]
        assert barge_ins
        reactions = [p.reaction_ms for p in barge_ins]
        assert all(r is not None and abs(r - reaction_ms) <= TOLERANCE_MS for r in reactions)
        interrupted = [p for _, p in sink.utterances("agent") if p.interrupted]
        assert interrupted


async def test_events_follow_adr_0006_timing_and_links() -> None:
    result, sink, stats = await _call(_loopback(reply_after_ms=400), voice_trial(), LINES[:2])
    assert (result.status, result.end_reason, result.turns) == ("ok", EndReason.GOAL_ACHIEVED, 4)
    ids = {event.id: event for event in sink.events}
    turns = sorted(sink.utterances("user") + sink.utterances("agent"), key=lambda e: e[1].turn)
    assert [p.turn for _, p in turns] == [0, 1, 2, 3]
    assert [event.actor for event, _ in turns] == ["user", "agent", "user", "agent"]
    for event, payload in turns:
        assert payload.audio_span_id is not None
        span = ids[payload.audio_span_id].payload
        assert isinstance(span, AudioSpanPayload)
        assert span.start_ms == event.t_ms
        assert payload.end_ms is not None
        assert payload.end_ms >= event.t_ms
    agent_turns = [p for _, p in sink.utterances("agent")]
    assert all(p.text == SHORT_REPLY_TEXT for p in agent_turns)
    assert all(p.asr_confidence == 1.0 for p in agent_turns)
    gaps = sink.silences()
    assert {(gap.attributed_to, gap.reason) for gap in gaps} == {
        ("agent", "turn_gap"),
        ("user", "turn_gap"),
    }
    caller_gaps = [gap.end_ms - gap.start_ms for gap in gaps if gap.attributed_to == "user"]
    # Never sooner than the persona's 200-300 ms; later only by the time ASR, the simulator and
    # TTS take once the agent has stopped (ADR 0009a), which is slow on small CI runners.
    assert caller_gaps
    assert all(200 <= gap <= 300 + PROCESSING_ALLOWANCE_MS for gap in caller_gaps), caller_gaps
    [call] = stats
    assert call.frames_sent > 100
    assert call.frames_received > 100


async def test_simulator_hears_the_agent_through_asr() -> None:
    sink = RecordingSink()
    customer = ScriptedCaller(LINES[:2])
    driver = VoiceDriver(_loopback(reply_after_ms=300), asr=FakeASR(), settings=FAST)
    await driver.run(context(), voice_trial(), customer, sink)
    last = customer.seen[-1]
    assert [(turn.speaker, turn.text) for turn in last] == [
        ("user", LINES[0]),
        ("agent", SHORT_REPLY_TEXT),
        ("user", LINES[1]),
        ("agent", SHORT_REPLY_TEXT),
    ]


async def test_greeting_is_an_agent_turn_without_latency() -> None:
    target = _loopback(
        reply_after_ms=300, greeting=synthetic_reply("hello you are through to acme")
    )
    _, sink, _ = await _call(target, voice_trial(), LINES[:1], settings=VoiceSettings())
    [(_, greeting), (_, reply)] = sink.utterances("agent")
    assert greeting.turn == 0
    assert greeting.latency_ms is None
    assert greeting.text == "hello you are through to acme"
    assert reply.latency_ms is not None
    [(user_event, _)] = sink.utterances("user")
    assert user_event.t_ms > (greeting.end_ms or 0)


async def test_hesitation_pause_exposes_premature_endpointing() -> None:
    target = _loopback(reply_after_ms=300, endpoint_ms=150, barge_in_reaction_ms=None)
    trial = voice_trial(behaviors={"mid_utterance_pause_s": [1.0, 1.0]}, seed=4)
    lines = ["I would like to move my boiler visit to next week please."]
    _, sink, _ = await _call(target, trial, lines)
    hesitations = [gap for gap in sink.silences() if gap.reason == "hesitation"]
    assert hesitations
    assert all(gap.attributed_to == "user" for gap in hesitations)
    assert all(abs(gap.end_ms - gap.start_ms - 1000) <= 60 for gap in hesitations)
    false_interruptions = measure(VoiceMetric.FALSE_INTERRUPTIONS, sink.events, duration_ms=1)
    assert not isinstance(false_interruptions, str)
    assert false_interruptions.value > 0


async def test_fillers_are_their_own_spans_and_not_in_the_transcript() -> None:
    trial = voice_trial(behaviors={"fillers": True}, seed=0)
    lines = [f"Line number {index} about my visit." for index in range(6)]
    _, sink, _ = await _call(_loopback(reply_after_ms=300), trial, lines, settings=FAST)
    fillers = [span for span in sink.spans("user") if span.content == "filler"]
    assert fillers
    assert [p.text for _, p in sink.utterances("user")] == lines


async def test_backchannels_stopping_the_agent_are_flagged() -> None:
    # The loopback answers the "mm-hm" itself 800 ms later: past the hangover, so the cut shows.
    stopping = _loopback(text=LONG_REPLY_TEXT, reply_after_ms=800, barge_in_reaction_ms=200)
    ignoring = _loopback(text=LONG_REPLY_TEXT, reply_after_ms=300, barge_in_reaction_ms=None)
    trial = voice_trial(behaviors={"backchannel_prob": 1.0})
    outcomes: dict[str, RecordingSink] = {}

    async def run(name: str, target: LoopbackVoiceTarget) -> None:
        outcomes[name] = (await _call(target, trial, LINES[:1]))[1]

    async with anyio.create_task_group() as group:
        group.start_soon(run, "stopping", stopping)
        group.start_soon(run, "ignoring", ignoring)
    stopped = [p for _, p in outcomes["stopping"].interrupts() if p.style == "backchannel"]
    ignored = [p for _, p in outcomes["ignoring"].interrupts() if p.style == "backchannel"]
    assert stopped
    assert ignored
    assert all(
        p.reaction_ms is not None and abs(p.reaction_ms - 200) <= TOLERANCE_MS for p in stopped
    )
    assert all(p.reaction_ms is None for p in ignored)
    assert all(span.content == "backchannel" for span in outcomes["ignoring"].spans("user")[1:])
    # A backchannel is not a turn: the transcript only has the scripted line and replies.
    assert [p.text for _, p in outcomes["ignoring"].utterances("user")] == LINES[:1]


async def test_ignored_barge_in_counts_until_the_agent_stops() -> None:
    target = _loopback(text=LONG_REPLY_TEXT, reply_after_ms=300, barge_in_reaction_ms=None)
    trial = voice_trial(behaviors={"interrupt_prob": 1.0}, max_turns=4)
    result, sink, _ = await _call(target, trial, LINES[:1])
    assert result.status == "ok"
    barge_ins = [p for _, p in sink.interrupts() if p.style == "barge_in"]
    assert barge_ins
    assert all(p.reaction_ms is not None and p.reaction_ms > 1000 for p in barge_ins)


@pytest.mark.slow
async def test_re_engagement_silence_is_logged_as_the_callers() -> None:
    trial = voice_trial(behaviors={"silence_prob": 1.0})
    result, sink, _ = await _call(_loopback(reply_after_ms=300), trial, LINES[:2])
    assert result.status == "ok"
    [silence] = [gap for gap in sink.silences() if gap.reason == "re_engagement"]
    assert silence.attributed_to == "user"
    assert 5000 <= silence.end_ms - silence.start_ms <= 10000 + TOLERANCE_MS


async def test_limits_end_the_call_between_turns() -> None:
    by_turns, _, _ = await _call(_loopback(reply_after_ms=300), voice_trial(max_turns=3), LINES)
    assert (by_turns.status, by_turns.end_reason, by_turns.turns) == ("ok", EndReason.MAX_TURNS, 2)
    target = _loopback(text=LONG_REPLY_TEXT, reply_after_ms=300)
    by_time, sink, _ = await _call(target, voice_trial(max_duration_s=4), LINES)
    assert by_time.status == "timeout"
    assert by_time.end_reason == EndReason.MAX_DURATION
    assert any("max_duration_s" in message for message in sink.errors("agent"))


async def test_closing_text_is_said_before_ending() -> None:
    end = SimEnd(reason=EndReason.GOAL_ABANDONED, closing_text="Forget it, bye.")
    result, sink, _ = await _call(_loopback(reply_after_ms=300), voice_trial(), LINES[:1], end=end)
    assert result.end_reason == EndReason.GOAL_ABANDONED
    assert [p.text for _, p in sink.utterances("user")] == [LINES[0], "Forget it, bye."]


async def test_silent_agent_times_out_with_dead_air_logged() -> None:
    target = _loopback(reply_after_ms=60_000)
    result, sink, _ = await _call(target, voice_trial(), LINES, ctx=context(timeout_s=1.0))
    assert (result.status, result.end_reason) == ("timeout", EndReason.ERROR)
    assert result.error is not None
    assert "did not answer within 1.0 s" in result.error
    dead_air = measure(VoiceMetric.DEAD_AIR, sink.events, duration_ms=1)
    assert not isinstance(dead_air, str)
    assert dead_air.value == 0  # 1 s of silence is a timeout, not dead air (> 2 s)
    [gap] = [g for g in sink.silences() if g.attributed_to == "agent"]
    assert gap.end_ms - gap.start_ms >= 1000


async def test_agent_hanging_up_ends_the_call_normally() -> None:
    target = _loopback(reply_after_ms=300, hang_up_after_replies=1)
    result, sink, _ = await _call(target, voice_trial(), LINES)
    assert (result.status, result.end_reason) == ("ok", EndReason.AGENT_ENDED)
    assert not sink.errors("agent")


async def test_recordings_hold_both_sides_on_the_trial_timeline(tmp_path: Path) -> None:
    _, sink, _ = await _call(
        _loopback(reply_after_ms=300), voice_trial(), LINES[:1], run_dir=tmp_path
    )
    [directory] = (tmp_path / "audio").glob("*/*")
    tracks = {}
    for name in ("caller", "agent", "mix"):
        with wave.open(str(directory / f"{name}.wav")) as recording:
            assert recording.getframerate() == SAMPLE_RATE
            frames = recording.readframes(recording.getnframes())
            tracks[name] = np.frombuffer(frames, dtype="<i2")
    assert len(tracks["mix"]) == 2 * len(tracks["caller"]) == 2 * len(tracks["agent"])
    sides: tuple[tuple[Actor, str], ...] = (("user", "caller"), ("agent", "agent"))
    for actor, track in sides:
        for span in sink.spans(actor):
            assert span.recording is not None
            assert span.recording.endswith(f"{track}.wav")
            assert span.start_sample is not None
            onset = tracks[track][span.start_sample : span.start_sample + 160]
            assert np.abs(onset).max() > 30
            before = tracks[track][span.start_sample - 160 : span.start_sample]
            assert np.abs(before).max() < SPEECH_AMPLITUDE * 32768


async def test_noisy_impaired_persona_runs_and_sends_noise_between_turns() -> None:
    voice = {
        # Quiet enough for the loopback's energy endpointing to hear the caller stop.
        "environment": "street",
        "snr_db": 25,
        "reverb": "office",
        "line": "g711_mulaw_8k",
        "packet_loss": 0.05,
        "jitter_ms": 15,
    }
    stats: list[CallStats] = []
    driver = VoiceDriver(
        _loopback(reply_after_ms=300, barge_in_reaction_ms=None),
        asr=FakeASR(),
        settings=FAST,
        on_call_end=stats.append,
    )
    result = await driver.run(
        context(), voice_trial(voice=voice), ScriptedCaller(LINES[:1]), RecordingSink()
    )
    assert result.status == "ok"
    [call] = stats
    assert call.lost_frames > 0


# --- agent failures --------------------------------------------------------------------------


class _Target:
    def __init__(self, link: AudioLink | None = None, error: Exception | None = None) -> None:
        self._link = link
        self._error = error

    async def connect(self, ctx: SessionContext) -> AudioLink:
        if self._error is not None:
            raise self._error
        if self._link is None:
            await anyio.sleep(60)
        assert self._link is not None
        return self._link


class _BrokenLink:
    """Fails sending after ``send_ok`` frames, or its frame stream fails / changes rate."""

    def __init__(
        self,
        *,
        send_ok: int = 10**9,
        stream_error: Exception | None = None,
        rate: int = SAMPLE_RATE,
    ) -> None:
        self._send_ok = send_ok
        self._stream_error = stream_error
        self._rate = rate
        self.hung_up = False

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        self._send_ok -= 1
        if self._send_ok < 0:
            raise AgentError("connection reset by agent")

    async def frames(self) -> AsyncIterator[TimedFrame]:
        for index in range(1000):
            await anyio.sleep(0.02)
            if self._stream_error is not None and index == 5:
                raise self._stream_error
            yield TimedFrame(
                bytes(640 if self._rate == SAMPLE_RATE else 1920), self._rate, index * 320, 0
            )

    async def hangup(self) -> None:
        self.hung_up = True
        raise AgentError("already gone")


@pytest.mark.parametrize(
    ("target", "status", "message"),
    [
        (_Target(error=AgentError("busy signal")), "agent_error", "busy signal"),
        (_Target(error=AgentTimeoutError("no answer")), "timeout", "no answer"),
        (_Target(), "timeout", "did not answer the call within 0.3 s"),
        (_Target(_BrokenLink(send_ok=20)), "agent_error", "connection reset"),
        (
            _Target(_BrokenLink(stream_error=AgentError("stream broke"))),
            "agent_error",
            "stream broke",
        ),
    ],
)
async def test_agent_failures_are_reported_not_raised(
    target: _Target, status: str, message: str
) -> None:
    result, sink, _ = await _call(target, voice_trial(), LINES, ctx=context(timeout_s=0.3))
    assert result.status == status
    assert result.end_reason == EndReason.ERROR
    assert result.error is not None
    assert message in result.error
    assert any(message in error for error in sink.errors("agent"))


async def test_harness_failures_propagate() -> None:
    with pytest.raises(ValueError, match="16000 Hz"):
        await _call(_Target(_BrokenLink(rate=48_000)), voice_trial(), LINES)
    with pytest.raises(ValueError, match="no voice profile"):
        await _call(_loopback(), voice_trial(with_voice=False), LINES)


async def test_agent_transcript_payloads_are_valid_utterances() -> None:
    _, sink, _ = await _call(_loopback(reply_after_ms=300), voice_trial(), LINES[:1])
    for event, payload in sink.utterances("agent"):
        assert isinstance(payload, UtterancePayload)
        UtterancePayload.model_validate(payload.model_dump())
        assert event.actor == "agent"
