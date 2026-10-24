"""The PR comment (US-05): verdict, Δ with CI, flips grouped by cause, latency deltas, link.

Every section has a fixed line budget, so the comment stays under 30 lines (at most 27) however
many scenarios flip or slices regress; what does not fit is summarized as "+N more".
"""

import re
from collections.abc import Sequence
from typing import Final

from hecklr_report import fmt
from hecklr_report.model import Analysis, FlipDirection, FlipGroup
from hecklr_report.voice import RESPONSE_LATENCY, quantile_metric
from hecklr_schemas import Comparison, Verdict
from hecklr_stats import PASS_RATE

MAX_CAUSES: Final = {"pass_to_fail": 5, "fail_to_pass": 2}
MAX_SCENARIOS_PER_CAUSE: Final = 4
MAX_SLICES: Final = 3
MAX_LATENCY_LINES: Final = 3
RATIONALE_CHARS: Final = 90

_TITLES: Final = {
    Verdict.REGRESSION: "REGRESSION",
    Verdict.IMPROVEMENT: "IMPROVEMENT",
    Verdict.NO_CHANGE: "NO CHANGE",
    Verdict.INCONCLUSIVE: "INCONCLUSIVE",
}
_ARROWS: Final[dict[FlipDirection, str]] = {
    "pass_to_fail": "Flipped pass → fail",
    "fail_to_pass": "Flipped fail → pass",
}
_MARKDOWN_SPECIAL = re.compile(r"([\\`*_{}\[\]<>()#+|!~])")
_ZERO_WIDTH_JOINER: Final = "\N{ZERO WIDTH JOINER}"


def render_markdown(analysis: Analysis, *, report_url: str | None = None) -> str:
    """The PR comment for ``analysis``, under 30 lines, with a link to the full report."""
    lines = [_title(analysis), ""]
    lines += _headline(analysis)
    if analysis.baseline is not None:
        for direction in _ARROWS:
            lines += _flips(analysis.flips, direction)
        lines += _latency(analysis.comparisons)
        lines += _slices(analysis.comparisons)
    else:
        lines += _failing(analysis)
    lines += _health(analysis)
    if report_url is not None:
        lines.append(f"[Full report]({report_url})")
    return "\n".join(lines) + "\n"


def escape(text: str) -> str:
    """User or model text made inert in GitHub Markdown (no markup, links, HTML or mentions).

    A backslash does not stop GitHub from expanding ``@team`` into a notification, so a joiner
    after every ``@`` breaks the mention while the text still reads the same.
    """
    flat = _MARKDOWN_SPECIAL.sub(r"\\\1", " ".join(text.split()))
    return flat.replace("@", "@" + _ZERO_WIDTH_JOINER)


def _code(text: str) -> str:
    # Inside a code span nothing is markup; a backtick in the text would end the span early.
    return "`" + " ".join(text.replace("`", "'").split()) + "`"


def _title(analysis: Analysis) -> str:
    verdict = _TITLES[analysis.verdict] if analysis.verdict else "RUN SUMMARY"
    if analysis.problems:
        verdict = "CANNOT DECIDE"
    suite = f" · suite {_code(analysis.candidate.suite)}" if analysis.candidate.suite else ""
    return f"### Hecklr · {verdict}{suite} · agent {_code(analysis.candidate.agent.name)}"


def _headline(analysis: Analysis) -> list[str]:
    comparison = analysis.suite_comparison
    if comparison is not None:
        lines = [
            f"**Pass rate** {fmt.estimate(comparison.baseline_estimate, 'percent')} → "
            f"{fmt.estimate(comparison.candidate_estimate, 'percent')}: "
            f"**{fmt.delta(comparison)}** ({fmt.confidence(comparison.delta)}), "
            f"{fmt.p_equals(comparison.p_value)} · {comparison.n_scenarios} scenarios, "
            f"{comparison.n_trials_baseline} vs {comparison.n_trials_candidate} trials · "
            f"\N{GREEK SMALL LETTER DELTA}={100 * comparison.min_effect:g} pts, "
            f"\N{GREEK SMALL LETTER ALPHA}={comparison.alpha:g}"
        ]
        if comparison.verdict is Verdict.INCONCLUSIVE:
            lines.append(_inconclusive_note(comparison))
        return [*lines, ""]
    rate = analysis.summary(PASS_RATE)
    if rate is None:
        return []
    k_rates = [
        f"{metric} {fmt.estimate(e, 'percent')}"
        for metric in ("pass^3", "pass@3")
        if (e := analysis.summary(metric)) is not None
    ]
    extra = f" · {' · '.join(k_rates)}" if k_rates else ""
    return [
        f"**Pass rate** {fmt.estimate(rate, 'percent')} ({fmt.confidence(rate)}), "
        f"{rate.n} scenarios{extra}",
        "",
    ]


def _inconclusive_note(comparison: Comparison) -> str:
    if comparison.trials_needed is None:
        return "Inconclusive: more trials are unlikely to decide it; add scenarios."
    return (
        f"Inconclusive: about {comparison.trials_needed} more trials per scenario in each run "
        "would likely decide it."
    )


def _flips(groups: Sequence[FlipGroup], direction: FlipDirection) -> list[str]:
    chosen = [g for g in groups if g.direction == direction]
    if not chosen:
        return []
    total = sum(len(g.scenario_ids) for g in chosen)
    lines = [f"**{_ARROWS[direction]}: {total} scenario{'s' if total != 1 else ''}**"]
    limit = MAX_CAUSES[direction]
    for group in chosen[:limit]:
        shown = ", ".join(_code(s) for s in group.scenario_ids[:MAX_SCENARIOS_PER_CAUSE])
        more = len(group.scenario_ids) - MAX_SCENARIOS_PER_CAUSE
        rationale = group.rationale and _rationale(group.rationale)
        lines.append(
            f"- {_code(group.cause)} ({len(group.scenario_ids)}): {shown}"
            + (f" +{more} more" if more > 0 else "")
            + (f" — _{rationale}_" if rationale else "")
        )
    if len(chosen) > limit:
        hidden = chosen[limit:]
        count = sum(len(g.scenario_ids) for g in hidden)
        lines.append(f"- +{len(hidden)} more causes ({count} scenarios)")
    return [*lines, ""]


def _rationale(text: str) -> str:
    """A judge or grader rationale shortened to fit one comment line, escaped."""
    flat = " ".join(text.split())
    if len(flat) > RATIONALE_CHARS:
        flat = flat[: RATIONALE_CHARS - 1].rstrip() + "…"
    return escape(flat)


def _latency(comparisons: Sequence[Comparison]) -> list[str]:
    p95 = quantile_metric(RESPONSE_LATENCY, "p95")
    chosen = [c for c in comparisons if c.metric == p95][:MAX_LATENCY_LINES]
    return [
        f"**Latency p95** {fmt.slice_label(c.slice)}: "
        f"{fmt.estimate(c.baseline_estimate, 'ms')} → "
        f"{fmt.estimate(c.candidate_estimate, 'ms')}, {fmt.delta(c)}, "
        f"{c.verdict.value.replace('_', ' ')}"
        for c in chosen
    ]


def _slices(comparisons: Sequence[Comparison]) -> list[str]:
    lines = []
    for verdict in (Verdict.REGRESSION, Verdict.IMPROVEMENT):
        hits = [
            c for c in comparisons if c.metric == PASS_RATE and c.slice and c.verdict is verdict
        ]
        if not hits:
            continue
        shown = ", ".join(
            f"{_code(fmt.slice_label(c.slice))} {fmt.delta(c)}" for c in hits[:MAX_SLICES]
        )
        more = f" +{len(hits) - MAX_SLICES} more" if len(hits) > MAX_SLICES else ""
        name = "regressed" if verdict is Verdict.REGRESSION else "improved"
        lines.append(f"**Slices {name}** (q < \N{GREEK SMALL LETTER ALPHA}): {shown}{more}")
    return lines


def _failing(analysis: Analysis) -> list[str]:
    failing = [s for s in analysis.scenarios if s.pass_rate is not None and s.pass_rate.value < 1]
    if not failing:
        return []
    by_cause: dict[str, list[str]] = {}
    for scenario in failing:
        cause = scenario.cause.cause if scenario.cause else "no failing criterion recorded"
        by_cause.setdefault(cause, []).append(scenario.scenario_id)
    ranked = sorted(by_cause.items(), key=lambda item: (-len(item[1]), item[0]))
    groups = [
        FlipGroup(direction="pass_to_fail", cause=cause, scenario_ids=ids) for cause, ids in ranked
    ]
    lines = _flips(groups, "pass_to_fail")
    lines[0] = f"**Scenarios with failures: {len(failing)}**"
    return lines


def _health(analysis: Analysis) -> list[str]:
    counts = analysis.counts
    parts = [f"{counts.analyzed} of {counts.total} trials analyzed"]
    if counts.harness_error:
        parts.append(f"{counts.harness_error} harness errors")
    if counts.undecided:
        parts.append(f"{counts.undecided} undecided (grade errors)")
    if counts.contradiction_rate is not None:
        abandoned = f", {fmt.abandoned(counts.guard_abandoned)}" if counts.guard_abandoned else ""
        parts.append(
            f"simulator contradictions {fmt.estimate(counts.contradiction_rate, 'percent')}"
            + abandoned
        )
    lines = ["**Run** " + " · ".join(parts)]
    untrusted = [j for j in analysis.judges if not j.trusted]
    if untrusted:
        lines.append(
            "**Untrusted judges**: "
            + ", ".join(
                f"{_code(j.grader)} κ={fmt.estimate(j.kappa, 'number')} "
                f"({escape(j.reason or 'kappa < 0.70')})"
                for j in untrusted[:4]
            )
        )
    if analysis.problems:
        lines.append("**Cannot gate:** " + "; ".join(escape(p) for p in analysis.problems[:3]))
    return lines
