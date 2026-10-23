"""The comparison report of a fix session, in Markdown (terminal, PR body, ``report.md``)."""

from collections.abc import Sequence

from hecklr_fix.fixer import FixOutcome, FixResult
from hecklr_fix.validation import regressions
from hecklr_report import fmt
from hecklr_schemas import Comparison, Patch, PatchStatus

_HEADLINES = {
    FixOutcome.FIXED: "a validated patch is ready for review",
    FixOutcome.NO_CANDIDATE_BEAT_BASELINE: "no candidate beat the baseline",
    FixOutcome.BASELINE_PASSES: "nothing to fix",
    FixOutcome.BUDGET_EXHAUSTED: "the budget ran out before any candidate was accepted",
}
_STATUS = {
    PatchStatus.PROPOSED: "not evaluated",
    PatchStatus.REJECTED_TARGETED: "rejected: no significant improvement",
    PatchStatus.REJECTED_GUARD: "rejected: guard regression",
    PatchStatus.ACCEPTED: "accepted",
}


def render_markdown(result: FixResult, *, guard_suite: str) -> str:
    """The session as Markdown: outcome, every candidate's statistics, the winning diff."""
    cluster = result.cluster
    lines = [
        f"# hecklr fix: {_HEADLINES[result.outcome]}",
        "",
        f"**Cluster** {cluster.id}: {cluster.label}",
        f"**Outcome** `{result.outcome.value}`: {result.message}.",
        f"**Runs** targeted baseline `{result.targeted_baseline or '-'}`, guard baseline "
        f"`{result.guard_baseline or '-'}` (suite `{guard_suite}`).",
        f"**Spent** ${result.spent_usd:.4f} (patch generation and every run; the agent's own "
        "model calls are not included).",
    ]
    if result.patches:
        lines += ["", "## Candidates", "", *_table(result.patches)]
        lines += ["", _method_note(result.patches)]
    if result.rejected:
        lines += ["", "Proposals dropped before any run:"]
        lines += [f"- proposal {item.index}: {item.reason}" for item in result.rejected]
    if result.winner is not None:
        winner = result.winner
        lines += [
            "",
            f"## Winning patch: candidate {winner.candidate_index}",
            "",
            winner.rationale,
            "",
            "```diff",
            winner.diff.rstrip("\n"),
            "```",
            "",
            "A human reviews and merges this patch; Hecklr never merges it.",
        ]
    return "\n".join(lines) + "\n"


def _table(patches: Sequence[Patch]) -> list[str]:
    rows = [
        "| # | files | status | targeted Δ pass rate | p | guard Δ pass rate | p | regressed |",
        "| --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for patch in patches:
        targeted, guard = _first(patch.targeted), _first(patch.guard)
        regressed = [fmt.slice_label(c.slice) for c in regressions(patch.guard)]
        rows.append(
            f"| {patch.candidate_index} | {', '.join(patch.files)} | {_STATUS[patch.status]} | "
            f"{_delta(targeted)} | {_p(targeted)} | {_delta(guard)} | {_p(guard)} | "
            f"{', '.join(regressed) or '-'} |"
        )
    return rows


def _first(comparisons: Sequence[Comparison]) -> Comparison | None:
    return comparisons[0] if comparisons else None


def _delta(comparison: Comparison | None) -> str:
    if comparison is None:
        return "-"
    return f"{fmt.delta(comparison)} ({comparison.verdict.value})"


def _p(comparison: Comparison | None) -> str:
    return fmt.p_value(comparison.p_value) if comparison is not None else "-"


def _method_note(patches: Sequence[Patch]) -> str:
    compared = next((c for p in patches for c in (*p.targeted, *p.guard)), None)
    if compared is None:
        return "No candidate was run."
    return (
        f"Paired by trial cell (scenario, persona, channel, trial index; same seed), "
        f"{fmt.confidence(compared.delta)}, sign-flip p-values, alpha ={compared.alpha:g}, "
        f"minimum effect {100 * compared.min_effect:g} pts. Targeted: kept on an improvement of "
        "the whole set. Guard: dropped on a regression of the suite or of any tag "
        "(Benjamini-Hochberg q-values across tags)."
    )
