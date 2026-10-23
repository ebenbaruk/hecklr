"""A leaderboard row (``results/<stack>.json``): every metric with its interval, and reproduction.

Metric names:

- ``task_success``: the suite pass rate (every success criterion of a scenario passed), each
  scenario weighted equally, cluster bootstrap or Wilson interval (``hecklr_stats``);
- ``judge:<rubric>``: the share of judged conversations each judge rubric passed, same interval;
- ``response_latency_p50`` / ``_p95``: chat time to first token, or voice end of caller speech to
  first agent audio, bootstrapped over conversations;
- voice only: ``barge_in_reaction_p50`` / ``_p95``, ``false_interruptions_per_100_turns``,
  ``dead_air_per_call``, ``talk_over_s_per_min`` (``hecklr_report.voice`` definitions);
- ``cost_per_conversation_usd``: Hecklr's own spend plus the agent's, per conversation, mean
  with a bootstrap interval.
"""

from collections.abc import Mapping, Sequence
from decimal import Decimal

import numpy as np
from pydantic import AwareDatetime

from hecklr.commands.bench.spec import StackSpec
from hecklr_core import RunResults
from hecklr_report import Analysis, TrialCounts
from hecklr_report.voice import (
    BARGE_IN_REACTION,
    DEAD_AIR,
    FALSE_INTERRUPTIONS,
    RESPONSE_LATENCY,
    TALK_OVER,
    mean_estimate,
    quantile_metric,
)
from hecklr_schemas import Estimate, FrozenModel, Scenario
from hecklr_schemas.common import SchemaVersion
from hecklr_stats import PASS_RATE, ScenarioCounts, TrialOutcome, suite_pass_rate
from hecklr_stats.intervals import derive_rng

TASK_SUCCESS = "task_success"
COST = "cost_per_conversation_usd"
LATENCY_METRICS = tuple(quantile_metric(RESPONSE_LATENCY, q) for q in ("p50", "p95"))
BARGE_IN_METRICS = tuple(quantile_metric(BARGE_IN_REACTION, q) for q in ("p50", "p95"))
TIMING_METRICS = frozenset((*LATENCY_METRICS, *BARGE_IN_METRICS))
VOICE_METRICS = (
    *LATENCY_METRICS,
    *BARGE_IN_METRICS,
    FALSE_INTERRUPTIONS,
    DEAD_AIR,
    TALK_OVER,
)


class Environment(FrozenModel):
    """Where and with what a row was measured."""

    hecklr: str
    python: str
    platform: str
    machine: str
    cpus: int
    git_sha: str | None
    packages: dict[str, str]
    """Versions of the speech and transport libraries the stacks use."""
    load_average: tuple[float, float]
    """1-minute load average when the run started and ended: local speech models answer slower
    on a busy machine, so a row measured under load is marked as such."""


class Spend(FrozenModel):
    """What one run cost, by who spent it (an accounting fact, not an estimate)."""

    hecklr_usd: Decimal
    """Simulated customers and judges."""
    agent_usd: Decimal
    """The agent's own models and cloud speech, as it reported them."""

    @property
    def total_usd(self) -> Decimal:
        """Both."""
        return self.hecklr_usd + self.agent_usd


class StackResult(FrozenModel):
    """One stack measured on one benchmark version."""

    schema_version: SchemaVersion = 1
    benchmark_version: str
    suite_digest: str
    """SHA-256 of ``bench.yaml`` and the suite files: rows with equal digests ran the same suite."""
    stack: StackSpec
    run_id: str
    created_at: AwareDatetime
    seed: int
    trials_per_scenario: int
    scenarios: int
    models: dict[str, str]
    environment: Environment
    counts: TrialCounts
    metrics: dict[str, Estimate]
    spend: Spend
    duration_s: float


def stack_metrics(
    analysis: Analysis,
    results: RunResults,
    scenarios: Mapping[str, Scenario],
    agent_spend: Mapping[str, Decimal],
    *,
    resamples: int,
) -> dict[str, Estimate]:
    """Every leaderboard metric of one analyzed run (see the module docstring).

    ``agent_spend`` maps trial ids to what the agent spent during that trial.
    """
    metrics: dict[str, Estimate] = {}
    seed = analysis.seed
    if (rate := analysis.summary(PASS_RATE)) is not None:
        metrics[TASK_SUCCESS] = rate
    metrics.update(judge_scores(results, scenarios, seed=seed, resamples=resamples))
    if analysis.voice is not None:
        found = {s.metric: s.estimate for s in analysis.voice.metrics if not s.slice}
        metrics.update({name: found[name] for name in VOICE_METRICS if name in found})
    else:
        for name in LATENCY_METRICS:
            if (latency := analysis.summary(name)) is not None:
                metrics[name] = latency
    costs = [
        float(trial.cost_usd + agent_spend.get(trial.id, Decimal(0)))
        for trial in results.trials
        if trial.status != "harness_error"
    ]
    if costs:
        metrics[COST] = mean_estimate(costs, seed=_seed(seed, COST), resamples=resamples)
    return metrics


def judge_scores(
    results: RunResults, scenarios: Mapping[str, Scenario], *, seed: int, resamples: int
) -> dict[str, Estimate]:
    """Per judge rubric, the share of conversations it passed, scenarios weighted equally.

    A conversation that did not end normally (agent error, timeout) fails every rubric, as it
    fails the trial; a judge that errored leaves that conversation out of its rubric.
    """
    grades = {(g.trial_id, g.criterion_index): g for g in results.grades}
    outcomes: dict[str, list[TrialOutcome]] = {}
    for trial in results.trials:
        scenario = scenarios.get(trial.scenario_id)
        if trial.status == "harness_error" or scenario is None:
            continue
        for index, criterion in enumerate(scenario.success):
            if criterion.type != "judge":
                continue
            grade = grades.get((trial.id, index))
            if grade is None or grade.status != "ok":
                continue
            outcomes.setdefault(f"judge:{criterion.rubric}", []).append(
                TrialOutcome(
                    scenario_id=trial.scenario_id, passed=grade.passed and trial.status == "ok"
                )
            )
    return {
        name: suite_pass_rate(
            ScenarioCounts.from_outcomes(found), seed=_seed(seed, name), resamples=resamples
        )
        for name, found in sorted(outcomes.items())
    }


def _seed(seed: int, metric: str) -> int:
    return int(derive_rng(seed, "bench", metric).integers(0, np.iinfo(np.int64).max))


class MetricCheck(FrozenModel):
    """One metric of a published row against the same metric re-measured."""

    metric: str
    published: Estimate
    reproduced: Estimate
    within: bool
    """The re-measured value lies inside the published interval."""
    consistent: bool
    """The two intervals overlap: no evidence the stack measures differently now."""


TIMING_TOLERANCE_MS = 20.0
"""How far apart two timing intervals may sit and still agree: the harness's own timing accuracy
(SC4). A row's bootstrap interval covers sampling noise within one run, not the few milliseconds
a machine's scheduling moves between runs, which dominate an agent that answers in milliseconds."""


def check_reproduction(published: StackResult, reproduced: StackResult) -> list[MetricCheck]:
    """Every metric both rows have, compared.

    A re-measurement is itself a sample, so its point estimate falls outside the published 95%
    interval more often than 5% of the time even when nothing changed; overlapping intervals
    (timing intervals widened by ``TIMING_TOLERANCE_MS``) are the reproduction criterion, and
    ``within`` is reported alongside.
    """
    checks = []
    for name in sorted(published.metrics.keys() & reproduced.metrics.keys()):
        old, new = published.metrics[name], reproduced.metrics[name]
        slack = TIMING_TOLERANCE_MS if name in TIMING_METRICS else 0.0
        checks.append(
            MetricCheck(
                metric=name,
                published=old,
                reproduced=new,
                within=old.ci_low <= new.value <= old.ci_high,
                consistent=(
                    old.ci_low - slack <= new.ci_high + slack
                    and new.ci_low - slack <= old.ci_high + slack
                ),
            )
        )
    return checks


def missing_metrics(published: StackResult, reproduced: StackResult) -> Sequence[str]:
    """Metrics of the published row the reproduction could not measure."""
    return sorted(published.metrics.keys() - reproduced.metrics.keys())
