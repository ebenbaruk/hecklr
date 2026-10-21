"""Voice metrics on synthetic event logs, including the signatures of faults F7, F8, F9 and F12."""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING

import pytest

from hecklr_grade.voice import TYPICAL_GATES, VoiceMetricGrader, measure
from hecklr_schemas import Channel, GradeContext
from hecklr_schemas.scenario import VoiceMetric

if TYPE_CHECKING:
    from conftest import ContextFactory, EventLog

pytestmark = pytest.mark.anyio

USER_TURN_MS = 2_000
AGENT_TURN_MS = 3_000


@dataclass(frozen=True)
class CallShape:
    """How the agent behaves on a synthetic six-turn call; the defaults are a clean agent."""

    response_ms: int = 450
    barge_in_reaction_ms: int | None = 180
    endpointing_ms: int | None = None
    """Set to cut in this long after a caller hesitation starts (premature endpointing)."""
    stall_ms: int | None = None
    """Set to make the agent go silent this long on one turn (an unhandled tool timeout)."""
    backchannel_stops: bool = False


def simulate_call(log: EventLog, shape: CallShape, turns: int = 6) -> int:
    """Append a call's events; returns its duration in ms."""
    t = 0
    for turn in range(turns):
        user_start = t
        if turn == 1:
            # A mid-utterance hesitation the agent must wait through.
            log.span("user", user_start, user_start + 800)
            log.silence(
                user_start + 800, user_start + 1_600, attributed_to="user", reason="hesitation"
            )
            if shape.endpointing_ms is not None:
                cut_in = user_start + 800 + shape.endpointing_ms
                log.span("agent", cut_in, cut_in + 400)
            log.span("user", user_start + 1_600, user_start + USER_TURN_MS + 400)
            user_end = user_start + USER_TURN_MS + 400
        else:
            log.span("user", user_start, user_start + USER_TURN_MS)
            user_end = user_start + USER_TURN_MS
        log.say("user", f"caller turn {turn}", user_start, end_ms=user_end)

        gap = shape.response_ms
        if turn == 3 and shape.stall_ms is not None:
            gap = shape.stall_ms
        agent_start = user_end + gap
        log.silence(user_end, agent_start, attributed_to="agent")
        agent_end = agent_start + AGENT_TURN_MS
        if turn == 2:
            barge_at = agent_start + 1_000
            reaction = shape.barge_in_reaction_ms
            agent_end = agent_end if reaction is None else barge_at + reaction
            log.span("user", barge_at, barge_at + 1_200)
            log.interrupt(barge_at, reaction_ms=reaction)
        if turn == 4:
            log.span("user", agent_start + 500, agent_start + 800, content="backchannel")
            log.interrupt(
                agent_start + 500,
                style="backchannel",
                reaction_ms=120 if shape.backchannel_stops else None,
            )
        log.span("agent", agent_start, agent_end)
        log.say("agent", f"agent turn {turn}", agent_start, end_ms=agent_end, latency_ms=gap)
        t = agent_end + 600
    return t


async def grade_all(
    log: EventLog, ctx_for: ContextFactory, shape: CallShape
) -> dict[VoiceMetric, bool]:
    duration = simulate_call(log, shape)
    ctx: GradeContext = ctx_for(log, channel=Channel.VOICE, duration_ms=duration)
    results = {}
    for metric in VoiceMetric:
        grade = await VoiceMetricGrader(metric).grade(ctx)
        assert grade.status == "ok", grade.rationale
        assert grade.criterion_index is None
        results[metric] = grade.passed
    return results


async def test_us06_clean_call_passes_every_typical_gate(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    assert all((await grade_all(log, ctx_for, CallShape())).values())


@pytest.mark.parametrize(
    ("fault", "shape", "failing"),
    [
        ("F7", CallShape(response_ms=450 + 700), {VoiceMetric.RESPONSE_LATENCY}),
        ("F8", CallShape(barge_in_reaction_ms=None), {VoiceMetric.BARGE_IN_REACTION}),
        ("F9", CallShape(endpointing_ms=150), {VoiceMetric.FALSE_INTERRUPTIONS}),
        # A silent agent is both dead air and, on that turn, a very late response.
        ("F12", CallShape(stall_ms=8_000), {VoiceMetric.DEAD_AIR, VoiceMetric.RESPONSE_LATENCY}),
        ("backchannel", CallShape(backchannel_stops=True), {VoiceMetric.BACKCHANNEL_ROBUSTNESS}),
    ],
)
async def test_sc8_fault_signature_fails_exactly_its_metrics(
    log: EventLog,
    ctx_for: ContextFactory,
    fault: str,
    shape: CallShape,
    failing: set[VoiceMetric],
) -> None:
    results = await grade_all(log, ctx_for, shape)
    assert {metric for metric, passed in results.items() if not passed} == failing, fault


async def test_f7_added_latency_is_measured_exactly(log: EventLog, ctx_for: ContextFactory) -> None:
    duration = simulate_call(log, CallShape(response_ms=1_150))
    criterion = {"type": "voice_metric", "metric": "response_latency", "below": 1000}
    grade = await VoiceMetricGrader(VoiceMetric.RESPONSE_LATENCY).grade(
        ctx_for(log, criterion, channel=Channel.VOICE, duration_ms=duration)
    )
    assert grade.value == 1_150.0
    assert not grade.passed
    assert grade.criterion_index == 0
    assert len(grade.evidence) == 6


async def test_f8_barge_in_never_stopping_counts_the_rest_of_the_agent_turn(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    duration = simulate_call(log, CallShape(barge_in_reaction_ms=None))
    result = measure(VoiceMetric.BARGE_IN_REACTION, log.events, duration_ms=duration)
    assert not isinstance(result, str)
    assert result.value == AGENT_TURN_MS - 1_000


async def test_barge_in_without_an_agent_span_runs_to_the_end_of_the_call(log: EventLog) -> None:
    log.say("user", "hello", 0, end_ms=500)
    log.interrupt(4_000, reaction_ms=None)
    result = measure(VoiceMetric.BARGE_IN_REACTION, log.events, duration_ms=10_000)
    assert not isinstance(result, str)
    assert result.value == 6_000


async def test_f9_false_interruption_rate_per_100_caller_turns(log: EventLog) -> None:
    duration = simulate_call(log, CallShape(endpointing_ms=150))
    result = measure(VoiceMetric.FALSE_INTERRUPTIONS, log.events, duration_ms=duration)
    assert not isinstance(result, str)
    assert result.value == pytest.approx(100 / 6)
    assert len(result.worst) == 1


async def test_f12_dead_air_counts_long_agent_gaps(log: EventLog) -> None:
    duration = simulate_call(log, CallShape(stall_ms=8_000))
    result = measure(VoiceMetric.DEAD_AIR, log.events, duration_ms=duration)
    assert not isinstance(result, str)
    assert result.value == 1.0


async def test_caller_gaps_are_not_dead_air(log: EventLog) -> None:
    log.silence(0, 9_000, attributed_to="user")
    log.silence(9_000, 18_000, attributed_to="user", reason="re_engagement")
    log.silence(18_000, 19_900, attributed_to="agent")
    result = measure(VoiceMetric.DEAD_AIR, log.events, duration_ms=20_000)
    assert not isinstance(result, str)
    assert result.value == 0.0


async def test_talk_over_ratio_is_overlap_seconds_per_minute(log: EventLog) -> None:
    log.span("user", 0, 10_000)
    log.span("agent", 8_000, 14_000)
    log.span("user", 13_000, 20_000)
    log.span("agent", 25_000, 26_000, content="filler")
    result = measure(VoiceMetric.TALK_OVER_RATIO, log.events, duration_ms=30_000)
    assert not isinstance(result, str)
    assert result.value == pytest.approx(3.0 / 0.5)
    assert len(result.worst) == 1


async def test_talk_over_is_reported_only_by_default(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.span("user", 0, 60_000)
    log.span("agent", 0, 60_000)
    log.say("user", "x", 0)
    grade = await VoiceMetricGrader(VoiceMetric.TALK_OVER_RATIO).grade(
        ctx_for(log, channel=Channel.VOICE, duration_ms=60_000)
    )
    assert grade.passed
    assert grade.value == 60.0
    assert TYPICAL_GATES[VoiceMetric.TALK_OVER_RATIO] is None


@pytest.mark.parametrize(
    ("metric", "reason"),
    [
        (VoiceMetric.RESPONSE_LATENCY, "no agent turn"),
        (VoiceMetric.BARGE_IN_REACTION, "never barged in"),
        (VoiceMetric.FALSE_INTERRUPTIONS, "no caller turns"),
        (VoiceMetric.BACKCHANNEL_ROBUSTNESS, "never backchanneled"),
        (VoiceMetric.TALK_OVER_RATIO, "no duration"),
    ],
)
async def test_metrics_without_data_are_skipped(
    log: EventLog, ctx_for: ContextFactory, metric: VoiceMetric, reason: str
) -> None:
    grade = await VoiceMetricGrader(metric).grade(
        ctx_for(log, channel=Channel.VOICE, duration_ms=0)
    )
    assert grade.status == "skipped"
    assert reason in (grade.rationale or "")


async def test_voice_metrics_are_skipped_on_chat(log: EventLog, ctx_for: ContextFactory) -> None:
    simulate_call(log, CallShape())
    grade = await VoiceMetricGrader(VoiceMetric.DEAD_AIR).grade(ctx_for(log))
    assert grade.status == "skipped"


async def test_quantile_choice_changes_the_value(log: EventLog, ctx_for: ContextFactory) -> None:
    for index, latency in enumerate([100, 200, 300, 400, 5_000]):
        log.say("agent", "a", index * 10_000, latency_ms=latency)
    criterion = {"type": "voice_metric", "metric": "response_latency", "below": 1000}
    grader = VoiceMetricGrader(VoiceMetric.RESPONSE_LATENCY)
    p50 = await grader.grade(ctx_for(log, {**criterion, "quantile": "p50"}, channel=Channel.VOICE))
    p95 = await grader.grade(ctx_for(log, criterion, channel=Channel.VOICE))
    assert (p50.value, p50.passed) == (300.0, True)
    assert (p95.value, p95.passed) == (5_000.0, False)


async def test_mismatched_metric_criterion_is_a_wiring_bug(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    criterion = {"type": "voice_metric", "metric": "dead_air", "below": 1}
    with pytest.raises(TypeError):
        await VoiceMetricGrader(VoiceMetric.RESPONSE_LATENCY).grade(
            ctx_for(log, criterion, channel=Channel.VOICE)
        )
