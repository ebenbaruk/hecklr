"""A fix session on disk: ``<output_dir>/fix/<fix id>/`` (ADR 0015a).

::

    cluster.json     the FailureCluster the session fixed
    patches.jsonl    one Patch per candidate that applied, with its final status and comparisons
    report.md        the comparison report
    winner.diff      the winning patch (only when there is one); ``git apply winner.diff``
    summary.json     FixSummary, written last: its presence marks a finished session
    runs/<run id>/   every run of the session (one store and one response cache)
    logs/<pass>.log  each agent process's output
"""

from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ConfigDict

from hecklr_fix.fixer import FixOutcome, FixResult

FIX_DIR: Final = "fix"
"""Fix sessions live in ``<output_dir>/fix/<fix id>/``."""
SUMMARY_FILE: Final = "summary.json"
WINNER_FILE: Final = "winner.diff"
REPORT_FILE: Final = "report.md"


class FixSummary(BaseModel):
    """``summary.json``: the session's outcome in one record."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    fix_id: str
    outcome: FixOutcome
    message: str
    cluster_id: str
    winner_patch_id: str | None
    winner_candidate: int | None
    targeted_baseline: str | None
    guard_baseline: str | None
    guard_suite: str
    rejected: dict[int, str]
    """Proposal index -> why it was dropped before any run."""
    spent_usd: Decimal


def write_session(directory: Path, result: FixResult, *, guard_suite: str, report: str) -> None:
    """Write a finished session's artifacts into ``directory`` (which may already hold its runs).

    Raises:
        FileExistsError: ``directory`` already holds a finished session (artifacts are never
            overwritten).
    """
    if (directory / SUMMARY_FILE).exists():
        raise FileExistsError(f"{directory} already holds a finished fix session")
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "cluster.json").write_text(
        result.cluster.model_dump_json(indent=2) + "\n", "utf-8"
    )
    (directory / "patches.jsonl").write_text(
        "".join(patch.model_dump_json() + "\n" for patch in result.patches), "utf-8"
    )
    (directory / REPORT_FILE).write_text(report, "utf-8")
    if result.winner is not None:
        (directory / WINNER_FILE).write_text(result.winner.diff, "utf-8")
    winner = result.winner
    summary = FixSummary(
        fix_id=result.fix_id,
        outcome=result.outcome,
        message=result.message,
        cluster_id=result.cluster.id,
        winner_patch_id=winner.id if winner is not None else None,
        winner_candidate=winner.candidate_index if winner is not None else None,
        targeted_baseline=result.targeted_baseline,
        guard_baseline=result.guard_baseline,
        guard_suite=guard_suite,
        rejected={item.index: item.reason for item in result.rejected},
        spent_usd=result.spent_usd,
    )
    # Written last: its presence marks a complete session for readers.
    (directory / SUMMARY_FILE).write_text(summary.model_dump_json(indent=2) + "\n", "utf-8")
