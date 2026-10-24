"""The terminal summary: Rich tables, every number with its interval.

User and model text (scenario titles, causes, rationales) is passed as ``Text``, never as console
markup, so a rationale containing ``[red]`` prints literally.
"""

from collections.abc import Sequence
from typing import Final

from rich.console import Console
from rich.table import Table
from rich.text import Text

from hecklr_report import fmt
from hecklr_report.analysis import COST_PER_TRIAL
from hecklr_report.model import Analysis, DegradationPoint, ScenarioResult, VoiceSection
from hecklr_report.voice import (
    BACKCHANNEL_STOPS,
    BARGE_IN_REACTION,
    DEAD_AIR,
    FALSE_INTERRUPTIONS,
    RESPONSE_LATENCY,
    TALK_OVER,
    quantile_metric,
)
from hecklr_schemas import Estimate, Slice, Verdict
from hecklr_stats import PASS_RATE

MAX_SCENARIO_ROWS: Final = 20

_VERDICT_STYLES: Final = {
    Verdict.REGRESSION: "bold red",
    Verdict.IMPROVEMENT: "bold green",
    Verdict.NO_CHANGE: "green",
    Verdict.INCONCLUSIVE: "yellow",
}
RUN_METRICS: Final = (
    PASS_RATE,
    "pass^3",
    "pass@3",
    *(quantile_metric(RESPONSE_LATENCY, q) for q in ("p50", "p95", "p99")),
    COST_PER_TRIAL,
)
VOICE_COLUMNS: Final = (
    (quantile_metric(RESPONSE_LATENCY, "p50"), "latency p50"),
    (quantile_metric(RESPONSE_LATENCY, "p95"), "latency p95"),
    (quantile_metric(RESPONSE_LATENCY, "p99"), "latency p99"),
    (quantile_metric(BARGE_IN_REACTION, "p95"), "barge-in p95"),
    (FALSE_INTERRUPTIONS, "false int. /100 turns"),
    (DEAD_AIR, "dead air /call"),
    (BACKCHANNEL_STOPS, "backchannel stops"),
    (TALK_OVER, "talk-over s/min"),
)


def render_terminal(analysis: Analysis, console: Console) -> None:
    """Print the run summary (and comparison, when there is a baseline) to ``console``."""
    console.print(_header(analysis))
    console.print(_run_metrics(analysis))
    if analysis.comparisons:
        console.print(_comparisons(analysis))
    scenarios = _scenarios(analysis.scenarios)
    if scenarios is not None:
        console.print(scenarios)
    if analysis.voice is not None:
        console.print(_degradation(analysis.voice.degradation, "Success by SNR (dB)"))
        if analysis.voice.by_accent:
            console.print(_degradation(analysis.voice.by_accent, "Success by accent"))
        console.print(_voice_metrics(analysis.voice))
    if analysis.judges:
        console.print(_judges(analysis))
    console.print(_health(analysis))


def _header(analysis: Analysis) -> Text:
    run = analysis.candidate
    text = Text.assemble(("Hecklr ", "bold"), f"run {run.id} · agent ", run.agent.name)
    if run.suite:
        text.append(f" · suite {run.suite}")
    if analysis.baseline is not None:
        text.append(f" · baseline {analysis.baseline.id}")
    if analysis.verdict is not None:
        text.append("\nVerdict: ")
        text.append(
            analysis.verdict.value.replace("_", " ").upper(), _VERDICT_STYLES[analysis.verdict]
        )
    return text


def _cell(estimate: Estimate | None, unit: fmt.Unit) -> str:
    return fmt.estimate(estimate, unit) if estimate is not None else "—"


def _run_metrics(analysis: Analysis) -> Table:
    has_baseline = analysis.baseline is not None
    table = Table(title="Run metrics (95% CI)")
    table.add_column("Metric")
    if has_baseline:
        table.add_column("Baseline", justify="right")
    table.add_column("Candidate" if has_baseline else "Value", justify="right")
    table.add_column("n", justify="right")
    for metric in RUN_METRICS:
        estimate = analysis.summary(metric)
        if estimate is None:
            continue
        unit = fmt.unit_of(metric)
        cells: list[str | Text] = [metric]
        if has_baseline:
            cells.append(_cell(analysis.baseline_summary(metric), unit))
        table.add_row(*cells, _cell(estimate, unit), str(estimate.n))
    return table


def _comparisons(analysis: Analysis) -> Table:
    table = Table(title="Comparisons (candidate \N{MINUS SIGN} baseline)")
    for name in ("Metric", "Slice", "Baseline", "Candidate", "Δ [CI]", "p", "q", "Verdict"):
        table.add_column(
            name, justify="left" if name in ("Metric", "Slice", "Verdict") else "right"
        )
    for c in analysis.comparisons:
        unit = fmt.unit_of(c.metric)
        table.add_row(
            c.metric,
            Text(fmt.slice_label(c.slice)),
            _cell(c.baseline_estimate, unit),
            _cell(c.candidate_estimate, unit),
            Text(fmt.delta(c)),
            fmt.p_value(c.p_value),
            fmt.p_value(c.q_value),
            Text(c.verdict.value.replace("_", " "), _VERDICT_STYLES[c.verdict]),
        )
    return table


def _scenarios(results: Sequence[ScenarioResult]) -> Table | None:
    notable = [s for s in results if s.flip or (s.pass_rate is not None and s.pass_rate.value < 1)]
    if not notable:
        return None
    notable.sort(key=lambda s: (s.flip != "pass_to_fail", s.pass_rate.value if s.pass_rate else 0))
    table = Table(title="Scenarios with failures or flips")
    for name in ("Scenario", "Pass rate (Wilson 95% CI)", "Baseline", "Flip", "Main cause"):
        table.add_column(name)
    for s in notable[:MAX_SCENARIO_ROWS]:
        table.add_row(
            Text(s.scenario_id),
            _cell(s.pass_rate, "percent"),
            _cell(s.baseline_pass_rate, "percent"),
            (s.flip or "").replace("_", " "),
            Text(s.cause.cause if s.cause else ""),
        )
    if len(notable) > MAX_SCENARIO_ROWS:
        table.caption = f"+{len(notable) - MAX_SCENARIO_ROWS} more scenarios in the HTML report"
    return table


def _degradation(points: Sequence[DegradationPoint], title: str) -> Table:
    table = Table(title=title)
    for name in ("Bucket", "Trials", "Pass rate (95% CI)", "Baseline"):
        table.add_column(name)
    for point in points:
        table.add_row(
            Text(point.label),
            str(point.trials),
            _cell(point.pass_rate, "percent"),
            _cell(point.baseline_pass_rate, "percent"),
        )
    return table


def _voice_metrics(voice: VoiceSection) -> Table:
    table = Table(title="Voice turn-taking (95% CI)")
    table.add_column("Slice")
    for _, label in VOICE_COLUMNS:
        table.add_column(label, justify="right")
    slices: list[Slice] = []
    for summary in voice.metrics:
        if summary.slice not in slices:
            slices.append(summary.slice)
    for slice_ in slices:
        cells = []
        for metric, _ in VOICE_COLUMNS:
            estimate = next(
                (m.estimate for m in voice.metrics if m.metric == metric and m.slice == slice_),
                None,
            )
            cells.append(_cell(estimate, fmt.unit_of(metric)))
        table.add_row(Text(fmt.slice_label(slice_).replace("suite", "all voice")), *cells)
    return table


def _judges(analysis: Analysis) -> Table:
    table = Table(title="Judge agreement with humans")
    for name in ("Rubric", "Version", "n", "κ (95% CI)", "Trusted"):
        table.add_column(name)
    for judge in analysis.judges:
        table.add_row(
            Text(judge.grader),
            judge.grader_version,
            str(judge.n),
            _cell(judge.kappa, "number"),
            Text("yes", "green")
            if judge.trusted
            else Text(f"NO ({judge.reason or 'kappa < 0.70'})", "bold red"),
        )
    return table


def _health(analysis: Analysis) -> Text:
    counts = analysis.counts
    text = Text(
        f"{counts.analyzed} of {counts.total} trials analyzed · "
        f"{counts.harness_error} harness errors · {counts.undecided} undecided "
        f"({counts.grade_errors} grade errors) · {counts.agent_error} agent errors · "
        f"{counts.timeout} timeouts"
    )
    if counts.contradiction_rate is not None:
        text.append(
            f"\nSimulator contradiction rate {fmt.estimate(counts.contradiction_rate, 'percent')}"
        )
    for problem in analysis.problems:
        text.append(f"\nCannot gate: {problem}", "bold red")
    return text
