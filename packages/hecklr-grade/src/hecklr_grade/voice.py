"""Voice turn-taking metrics, computed from trial events only (definitions on ``VoiceMetric``).

Every timing is on the harness clock that the voice engine stamped on the events; nothing here
reads the agent's own logs or the audio itself.
"""

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final, Literal

from hecklr_grade.base import make_grade, of_kind, quantile, skipped
from hecklr_schemas import (
    AudioSpanPayload,
    Channel,
    Event,
    Grade,
    GradeContext,
    InterruptPayload,
    SilencePayload,
    UtterancePayload,
)
from hecklr_schemas.scenario import VoiceMetric, VoiceMetricCriterion

Quantile = Literal["p50", "p95", "p99"]
_QUANTILES: Final[dict[Quantile, float]] = {"p50": 0.50, "p95": 0.95, "p99": 0.99}

DEAD_AIR_MIN_MS: Final = 2_000
"""A turn gap longer than this, attributed to the agent, is dead air."""

TYPICAL_GATES: Final[dict[VoiceMetric, float | None]] = {
    VoiceMetric.RESPONSE_LATENCY: 1_000.0,
    VoiceMetric.BARGE_IN_REACTION: 400.0,
    VoiceMetric.FALSE_INTERRUPTIONS: 3.0,
    VoiceMetric.BACKCHANNEL_ROBUSTNESS: 0.10,
    VoiceMetric.DEAD_AIR: 1.0,
    VoiceMetric.TALK_OVER_RATIO: None,
}
"""Thresholds ("below") for diagnostic voice grades: the spec's typical gates (Voice, Metrics).

Dead air's typical gate is a median of 0 per call, i.e. below 1 per call; talk-over is reported
only, so its diagnostic grade always passes.
"""


@dataclass(frozen=True, slots=True)
class Measurement:
    """One voice metric of one trial."""

    value: float
    detail: str
    worst: tuple[Event, ...]
    """Events that make the metric worse: slow turns, interruptions, silences."""


def measure(
    metric: VoiceMetric, events: Sequence[Event], *, duration_ms: int, q: Quantile = "p95"
) -> Measurement | str:
    """Measure ``metric`` on a trial's events, or say why it does not apply (a skip reason)."""
    match metric:
        case VoiceMetric.RESPONSE_LATENCY:
            return _response_latency(events, q)
        case VoiceMetric.BARGE_IN_REACTION:
            return _barge_in_reaction(events, duration_ms, q)
        case VoiceMetric.FALSE_INTERRUPTIONS:
            return _false_interruptions(events)
        case VoiceMetric.BACKCHANNEL_ROBUSTNESS:
            return _backchannel_robustness(events)
        case VoiceMetric.DEAD_AIR:
            return _dead_air(events)
        case VoiceMetric.TALK_OVER_RATIO:
            return _talk_over_ratio(events, duration_ms)


def _quantile_of(samples: list[tuple[Event, float]], q: Quantile, unit: str) -> Measurement:
    value = quantile([sample for _, sample in samples], _QUANTILES[q])
    return Measurement(
        value=value,
        detail=f"{q} {value:.0f} {unit} over {len(samples)} event(s)",
        worst=tuple(event for event, sample in samples if sample >= value),
    )


def _response_latency(events: Sequence[Event], q: Quantile) -> Measurement | str:
    samples = [
        (event, float(payload.latency_ms))
        for event, payload in of_kind(events, UtterancePayload)
        if event.actor == "agent" and payload.latency_ms is not None
    ]
    if not samples:
        return "no agent turn with a measured response latency"
    return _quantile_of(samples, q, "ms")


def _speech(events: Sequence[Event], actor: str) -> list[tuple[Event, AudioSpanPayload]]:
    return [
        (event, span)
        for event, span in of_kind(events, AudioSpanPayload)
        if event.actor == actor and span.content == "speech"
    ]


def _barge_in_reaction(events: Sequence[Event], duration_ms: int, q: Quantile) -> Measurement | str:
    agent_speech = _speech(events, "agent")
    samples: list[tuple[Event, float]] = []
    for event, interrupt in of_kind(events, InterruptPayload):
        if event.actor != "user" or interrupt.target != "agent" or interrupt.style != "barge_in":
            continue
        if interrupt.reaction_ms is not None:
            samples.append((event, float(interrupt.reaction_ms)))
            continue
        # The agent never stopped: it talked until its turn ended (or the call did), so that is
        # how long the caller was talked over.
        ends = [
            span.end_ms for _, span in agent_speech if span.start_ms <= event.t_ms < span.end_ms
        ]
        samples.append((event, float(max(ends, default=max(duration_ms, event.t_ms)) - event.t_ms)))
    if not samples:
        return "the caller never barged in"
    return _quantile_of(samples, q, "ms")


def _false_interruptions(events: Sequence[Event]) -> Measurement | str:
    caller_turns = sum(1 for event, _ in of_kind(events, UtterancePayload) if event.actor == "user")
    if not caller_turns:
        return "no caller turns"
    hesitations = [
        pause for _, pause in of_kind(events, SilencePayload) if pause.reason == "hesitation"
    ]
    # Inclusive end: when the agent's own speech is what ended the hesitation, the silence ends
    # exactly where the agent started.
    offending = tuple(
        event
        for event, span in _speech(events, "agent")
        if any(pause.start_ms <= span.start_ms <= pause.end_ms for pause in hesitations)
    )
    value = 100.0 * len(offending) / caller_turns
    return Measurement(
        value=value,
        detail=(
            f"{len(offending)} agent turn(s) started in {len(hesitations)} caller hesitation(s); "
            f"{value:.1f} per 100 caller turns"
        ),
        worst=offending,
    )


def _backchannel_robustness(events: Sequence[Event]) -> Measurement | str:
    backchannels = [
        (event, interrupt)
        for event, interrupt in of_kind(events, InterruptPayload)
        if event.actor == "user" and interrupt.style == "backchannel"
    ]
    if not backchannels:
        return "the caller never backchanneled"
    stopped = tuple(event for event, interrupt in backchannels if interrupt.reaction_ms is not None)
    value = len(stopped) / len(backchannels)
    return Measurement(
        value=value,
        detail=f"{len(stopped)} of {len(backchannels)} backchannel(s) wrongly stopped the agent",
        worst=stopped,
    )


def _dead_air(events: Sequence[Event]) -> Measurement:
    gaps = tuple(
        event
        for event, pause in of_kind(events, SilencePayload)
        if pause.attributed_to == "agent"
        and pause.reason == "turn_gap"
        and pause.end_ms - pause.start_ms > DEAD_AIR_MIN_MS
    )
    return Measurement(
        value=float(len(gaps)),
        detail=f"{len(gaps)} agent silence(s) over {DEAD_AIR_MIN_MS} ms",
        worst=gaps,
    )


def _merged(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    merged: list[tuple[int, int]] = []
    for start, end in sorted(spans):
        if merged and start <= merged[-1][1]:
            merged[-1] = (merged[-1][0], max(merged[-1][1], end))
        else:
            merged.append((start, end))
    return merged


def _overlap_ms(left: list[tuple[int, int]], right: list[tuple[int, int]]) -> int:
    return sum(
        max(0, min(a_end, b_end) - max(a_start, b_start))
        for a_start, a_end in left
        for b_start, b_end in right
    )


def _talk_over_ratio(events: Sequence[Event], duration_ms: int) -> Measurement | str:
    if duration_ms <= 0:
        return "the call has no duration"
    user = _merged([(span.start_ms, span.end_ms) for _, span in _speech(events, "user")])
    agent = _speech(events, "agent")
    overlap_ms = _overlap_ms(user, _merged([(span.start_ms, span.end_ms) for _, span in agent]))
    value = (overlap_ms / 1000.0) / (duration_ms / 60_000.0)
    return Measurement(
        value=value,
        detail=f"{overlap_ms} ms of overlapping speech; {value:.2f} s per minute",
        worst=tuple(
            event for event, span in agent if _overlap_ms(user, [(span.start_ms, span.end_ms)]) > 0
        ),
    )


class VoiceMetricGrader:
    """A voice metric against a threshold: the criterion's ``below``, or the typical gate.

    Without a ``voice_metric`` criterion in the context it runs as a diagnostic, gated on
    ``TYPICAL_GATES`` at p95.
    """

    version = "1"

    def __init__(self, metric: VoiceMetric) -> None:
        self.metric = metric
        self.name = f"voice:{metric.value}"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Measure the metric on a voice trial; other channels are skipped."""
        if ctx.trial.channel is not Channel.VOICE:
            return skipped(ctx, self, f"{self.metric.value} is measured on voice calls only")
        criterion = ctx.criterion if isinstance(ctx.criterion, VoiceMetricCriterion) else None
        if criterion is not None and criterion.metric is not self.metric:
            raise TypeError(f"{self.name} got a criterion for {criterion.metric.value}")
        result = measure(
            self.metric,
            ctx.events,
            duration_ms=ctx.trial.duration_ms,
            q=criterion.quantile if criterion is not None else "p95",
        )
        if isinstance(result, str):
            return skipped(ctx, self, result)
        below = criterion.below if criterion is not None else TYPICAL_GATES[self.metric]
        passed = below is None or result.value < below
        gate = "reported only" if below is None else f"must be below {below:g}"
        return make_grade(
            ctx,
            self,
            passed=passed,
            value=result.value,
            rationale=f"{self.metric.value}: {result.detail}; {gate}",
            evidence=() if passed else result.worst,
        )
