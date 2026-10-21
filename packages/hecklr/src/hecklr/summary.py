"""The concise end-of-run summary ``hecklr run`` prints (US-01).

Every rate carries its interval: the suite pass rate a cluster bootstrap, each scenario's pass
rate and the simulator contradiction rate a Wilson interval, latency quantiles a bootstrap. The
full report (comparison, slices, flipped scenarios) is ``hecklr-report``'s job.
"""

from dataclasses import dataclass
from pathlib import Path

from rich.console import Console
from rich.markup import escape
from rich.table import Table

from hecklr.outcomes import trial_outcomes
from hecklr_core import RunResults
from hecklr_schemas import Estimate, Run, RunCounts, UtterancePayload
from hecklr_stats import PASS_RATE, ScenarioCounts, summarize_run, wilson_estimate

_SCENARIO_SLICE = "scenario"
_LATENCY_METRICS = ("response_latency_p50", "response_latency_p95")


@dataclass(frozen=True, slots=True)
class ScenarioRow:
    """One scenario's pass rate."""

    scenario_id: str
    passes: int
    trials: int
    pass_rate: Estimate


@dataclass(frozen=True, slots=True)
class RunSummary:
    """What a finished run looks like at a glance."""

    run: Run
    results_dir: Path
    suite_pass_rate: Estimate | None
    """``None`` when no trial could be counted (every one a harness or grading error)."""
    scenarios: list[ScenarioRow]
    latencies: dict[str, Estimate]
    """Agent response latency quantiles (chat: time to first token), by metric name."""
    harness_errors: int
    grading_errors: int
    contradictions: int
    customer_turns: int
    contradiction_rate: Estimate | None
    """Simulated customer turns the consistency guard had to regenerate, per turn."""

    @property
    def counts(self) -> RunCounts:
        """Trial statuses of the run."""
        return self.run.counts


def summarize(results: RunResults, *, results_dir: Path, resamples: int, seed: int) -> RunSummary:
    """Compute the summary of a stored run with ``hecklr_stats``."""
    counted = trial_outcomes(results)
    suite: Estimate | None = None
    scenarios: list[ScenarioRow] = []
    latencies: dict[str, Estimate] = {}
    if counted.outcomes:
        summaries = summarize_run(
            counted.outcomes, run_id=results.run.id, seed=seed, resamples=resamples
        )
        by_key = {(s.metric, tuple(sorted(s.slice.items()))): s.estimate for s in summaries}
        suite = by_key[(PASS_RATE, ())]
        latencies = {m: by_key[(m, ())] for m in _LATENCY_METRICS if (m, ()) in by_key}
        counts = ScenarioCounts.from_outcomes(counted.outcomes)
        scenarios = [
            ScenarioRow(
                scenario_id=sid,
                passes=int(passes),
                trials=int(trials),
                pass_rate=by_key[(PASS_RATE, ((_SCENARIO_SLICE, sid),))],
            )
            for sid, trials, passes in zip(
                counts.scenario_ids, counts.trials, counts.passes, strict=True
            )
        ]
    ran = [trial for trial in results.trials if trial.status != "harness_error"]
    ran_ids = {trial.id for trial in ran}
    contradictions = sum(trial.contradictions for trial in ran)
    customer_turns = sum(
        1
        for event in results.events
        if event.trial_id in ran_ids
        and event.actor == "user"
        and isinstance(event.payload, UtterancePayload)
    )
    contradiction_rate = None
    if customer_turns:
        # A regenerated turn is still sent once, so contradictions never exceed customer turns.
        contradiction_rate = wilson_estimate(min(contradictions, customer_turns), customer_turns)
    return RunSummary(
        run=results.run,
        results_dir=results_dir,
        suite_pass_rate=suite,
        scenarios=scenarios,
        latencies=latencies,
        harness_errors=counted.harness_errors,
        grading_errors=counted.grading_errors,
        contradictions=contradictions,
        customer_turns=customer_turns,
        contradiction_rate=contradiction_rate,
    )


def percent(estimate: Estimate) -> str:
    """A rate as ``83% [64%, 94%]``."""
    return f"{estimate.value:.0%} [{estimate.ci_low:.0%}, {estimate.ci_high:.0%}]"


def render_summary(summary: RunSummary, console: Console) -> None:
    """Print the summary: suite rate, per-scenario table, counts, run id and results path."""

    def line(text: str) -> None:
        # Never wrapped, so the run id and results path stay copyable on narrow terminals.
        console.print(text, soft_wrap=True)

    run = summary.run
    suite = summary.suite_pass_rate
    if suite is not None:
        line(
            f"[bold]Suite pass rate[/] {percent(suite)} ({suite.confidence:.0%} CI, "
            f"cluster bootstrap over {suite.n} scenarios)"
        )
    else:
        line("[bold red]No trial could be scored[/]: every trial failed to run or grade.")
    if summary.scenarios:
        table = Table(title="Scenarios (Wilson 95% CI)", title_justify="left")
        table.add_column("scenario")
        table.add_column("passed", justify="right")
        table.add_column("pass rate [95% CI]", justify="right")
        for row in summary.scenarios:
            table.add_row(row.scenario_id, f"{row.passes}/{row.trials}", percent(row.pass_rate))
        console.print(table)
    for metric, estimate in summary.latencies.items():
        line(
            f"{metric.replace('_', ' ')}: {estimate.value:.0f} ms "
            f"[{estimate.ci_low:.0f}, {estimate.ci_high:.0f}]"
        )
    counts = summary.counts
    line(
        f"Trials: {counts.finished}/{counts.planned} finished: {counts.ok} ok, "
        f"{counts.agent_error} agent errors, {counts.timeout} timeouts, "
        f"{counts.harness_error} harness errors (excluded), "
        f"{summary.grading_errors} grading errors (excluded)"
    )
    if summary.contradiction_rate is not None:
        line(
            f"Simulator contradictions: {summary.contradictions}/{summary.customer_turns} "
            f"customer turns regenerated, rate {percent(summary.contradiction_rate)}"
        )
    line(f"Spent: ${run.spent_usd:.4f} of budget {_budget(run)}")
    line(f"Run [bold]{run.id}[/] ({run.status.value})")
    line(f"Results: {escape(str(summary.results_dir))}")


def _budget(run: Run) -> str:
    return f"${run.budget.usd:.2f}" if run.budget.usd is not None else "unlimited"
