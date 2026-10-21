"""Voice turn-taking metrics per run and slice (US-06) and the degradation curve (US-07).

Per-trial values come from ``hecklr_grade.measure``, so a report and a ``voice_metric`` grade
always agree on a metric's definition. Run-level numbers resample whole trials (a call is the
independent unit), like ``hecklr_stats``'s latency quantiles.
"""

from collections.abc import Callable, Mapping, Sequence
from typing import Final

import numpy as np

from hecklr_grade import Measurement, measure
from hecklr_report.model import DegradationPoint, TrialRow
from hecklr_report.outcomes import ACCENT, CLEAN, SNR_BUCKETS, SNR_DB, agent_latencies
from hecklr_schemas import (
    AudioSpanPayload,
    Channel,
    Estimate,
    Event,
    InterruptPayload,
    IntervalMethod,
    MetricSummary,
    RunId,
    Slice,
    VoiceMetric,
)
from hecklr_stats import PASS_RATE, TrialOutcome, quantile_estimates
from hecklr_stats.intervals import (
    DEFAULT_CONFIDENCE,
    chunk_sizes,
    derive_rng,
    percentile_interval,
)
from hecklr_stats.outcomes import slice_key

QUANTILES: Final = {"p50": 0.50, "p95": 0.95, "p99": 0.99}
RESPONSE_LATENCY: Final = "response_latency"
BARGE_IN_REACTION: Final = "barge_in_reaction"
FALSE_INTERRUPTIONS: Final = "false_interruptions_per_100_turns"
DEAD_AIR: Final = "dead_air_per_call"
BACKCHANNEL_STOPS: Final = "backchannel_stop_rate"
TALK_OVER: Final = "talk_over_s_per_min"

PER_CALL_METRICS: Final = {
    FALSE_INTERRUPTIONS: VoiceMetric.FALSE_INTERRUPTIONS,
    DEAD_AIR: VoiceMetric.DEAD_AIR,
    BACKCHANNEL_STOPS: VoiceMetric.BACKCHANNEL_ROBUSTNESS,
    TALK_OVER: VoiceMetric.TALK_OVER_RATIO,
}
"""Metrics reported as the mean over calls of each call's value."""

SNR_ORDER: Final = (CLEAN, *(str(bucket) for bucket in SNR_BUCKETS))


def quantile_metric(base: str, q: str) -> str:
    """Metric name of one quantile, e.g. ``response_latency_p95``."""
    return f"{base}_{q}"


def voice_metrics(
    rows: Sequence[TrialRow],
    events: Mapping[str, Sequence[Event]],
    *,
    run_id: RunId,
    seed: int,
    resamples: int,
) -> list[MetricSummary]:
    """Turn-taking metrics of the voice trials, for all of them and per SNR bucket and accent.

    Harness-error trials are left out; undecided ones are kept, since timing needs no verdict.
    """
    calls = [r for r in rows if r.channel is Channel.VOICE and r.excluded != "harness_error"]
    slices: list[Slice] = [{}]
    for dimension in (SNR_DB, ACCENT):
        values = sorted({r.labels[dimension] for r in calls if dimension in r.labels})
        slices.extend({dimension: value} for value in values)
    summaries = []
    for slice_ in slices:
        members = [r for r in calls if all(r.labels.get(k) == v for k, v in slice_.items())]
        for metric, estimate in _slice_metrics(members, events, seed, slice_, resamples):
            summaries.append(
                MetricSummary(run_id=run_id, metric=metric, slice=slice_, estimate=estimate)
            )
    return summaries


def _slice_metrics(
    calls: Sequence[TrialRow],
    events: Mapping[str, Sequence[Event]],
    seed: int,
    slice_: Slice,
    resamples: int,
) -> list[tuple[str, Estimate]]:
    found: list[tuple[str, Estimate]] = []
    samplers: dict[str, Callable[[Sequence[Event], int], Sequence[float]]] = {
        RESPONSE_LATENCY: lambda evs, _: [float(v) for v in agent_latencies(evs)],
        BARGE_IN_REACTION: barge_in_reactions,
    }
    for base, sampler in samplers.items():
        values: list[float] = []
        groups: list[int] = []
        for i, call in enumerate(calls):
            samples = sampler(events.get(call.trial_id, ()), call.duration_ms)
            values.extend(samples)
            groups.extend([i] * len(samples))
        if values:
            estimates = quantile_estimates(
                values,
                list(QUANTILES.values()),
                seed=_seed(seed, base, slice_),
                resamples=resamples,
                groups=groups,
            )
            found.extend(
                (quantile_metric(base, q), e) for q, e in zip(QUANTILES, estimates, strict=True)
            )
    for name, metric in PER_CALL_METRICS.items():
        per_call = [
            m.value
            for call in calls
            if isinstance(
                m := measure(metric, events.get(call.trial_id, ()), duration_ms=call.duration_ms),
                Measurement,
            )
        ]
        if per_call:
            found.append(
                (name, mean_estimate(per_call, seed=_seed(seed, name, slice_), resamples=resamples))
            )
    return found


def barge_in_reactions(events: Sequence[Event], duration_ms: int) -> list[float]:
    """Barge-in reaction times of one call, in ms.

    Same definition as the ``barge_in_reaction`` grader (ADR 0004a): a barge-in the agent never
    reacted to lasts until the agent's speech ended, or the call did if no span covers it. The
    grader exposes only a per-call quantile, and a run-level quantile needs every sample.
    """
    agent_spans = [
        e.payload
        for e in events
        if e.actor == "agent"
        and isinstance(e.payload, AudioSpanPayload)
        and e.payload.content == "speech"
    ]
    reactions = []
    for event in events:
        payload = event.payload
        if not (
            event.actor == "user"
            and isinstance(payload, InterruptPayload)
            and payload.target == "agent"
            and payload.style == "barge_in"
        ):
            continue
        if payload.reaction_ms is not None:
            reactions.append(float(payload.reaction_ms))
            continue
        ends = [s.end_ms for s in agent_spans if s.start_ms <= event.t_ms < s.end_ms]
        reactions.append(float(max(ends, default=max(duration_ms, event.t_ms)) - event.t_ms))
    return reactions


def mean_estimate(
    values: Sequence[float], *, seed: int, resamples: int, confidence: float = DEFAULT_CONFIDENCE
) -> Estimate:
    """Mean of per-unit values with a percentile bootstrap interval over the units."""
    data = np.asarray(values, dtype=np.float64)
    if data.size == 0:
        raise ValueError("cannot estimate the mean of no values")
    rng = np.random.default_rng(seed)
    replicates = np.concatenate(
        [
            data[rng.integers(0, data.size, size=(size, data.size))].mean(axis=1)
            for size in chunk_sizes(resamples, data.size)
        ]
    )
    low, high = percentile_interval(replicates, confidence)
    point = float(data.mean())
    return Estimate(
        value=point,
        ci_low=min(low, point),
        ci_high=max(high, point),
        confidence=confidence,
        n=int(data.size),
        method=IntervalMethod.BOOTSTRAP,
    )


def degradation(
    dimension: str,
    order: Sequence[str] | None,
    outcomes: Sequence[TrialOutcome],
    summaries: Sequence[MetricSummary],
    baseline_summaries: Sequence[MetricSummary],
) -> list[DegradationPoint]:
    """Pass rate by value of ``dimension`` (``order`` first, then the rest sorted)."""
    trials: dict[str, int] = {}
    for outcome in outcomes:
        if dimension in outcome.labels:
            label = outcome.labels[dimension]
            trials[label] = trials.get(label, 0) + 1
    ranked = [label for label in order or () if label in trials]
    ranked += sorted(set(trials) - set(ranked))

    def find(source: Sequence[MetricSummary], label: str) -> Estimate | None:
        return next(
            (s.estimate for s in source if s.metric == PASS_RATE and s.slice == {dimension: label}),
            None,
        )

    points = []
    for label in ranked:
        estimate = find(summaries, label)
        if estimate is not None:
            points.append(
                DegradationPoint(
                    label=label,
                    trials=trials[label],
                    pass_rate=estimate,
                    baseline_pass_rate=find(baseline_summaries, label),
                )
            )
    return points


def _seed(seed: int, metric: str, slice_: Slice) -> int:
    """An independent stream per (metric, slice), as ``hecklr_stats.summarize_run`` does."""
    return int(derive_rng(seed, metric, slice_key(slice_)).integers(0, np.iinfo(np.int64).max))
