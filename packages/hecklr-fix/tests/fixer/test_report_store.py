from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from hecklr_fix.fixer import FixOutcome, FixResult
from hecklr_fix.generate import RejectedProposal
from hecklr_fix.report import render_markdown
from hecklr_fix.store import FixSummary, write_session
from hecklr_fix.validation import compare_cells
from hecklr_schemas import (
    ClusterId,
    FailureCluster,
    Patch,
    PatchId,
    PatchStatus,
    RunId,
    new_id,
)

from .factories import FAST_STATS, make_run, scenario, suite_run

NOW = datetime(2026, 10, 23, tzinfo=UTC)
CLUSTER = FailureCluster(
    id=ClusterId(new_id()), label="books without confirming", source="run", created_at=NOW
)
SCENARIOS = [scenario("book-a", ["booking"]), scenario("refund-a", ["refunds"])]


def _patch(index: int, status: PatchStatus, *, regress: bool = False) -> Patch:
    before = suite_run(make_run({"book-a": [False] * 6, "refund-a": [True] * 6}), SCENARIOS)
    after = suite_run(make_run({"book-a": [True] * 6, "refund-a": [not regress] * 6}), SCENARIOS)
    comparisons = compare_cells(before, after, stats=FAST_STATS, seed=1)
    return Patch(
        id=PatchId(new_id()),
        cluster_id=CLUSTER.id,
        candidate_index=index,
        files=["prompts/system.md"],
        diff="diff --git a/prompts/system.md b/prompts/system.md\n-old\n+new\n",
        rationale=f"Rationale {index}.",
        status=status,
        targeted=comparisons if status is not PatchStatus.PROPOSED else [],
        guard=comparisons if status in (PatchStatus.ACCEPTED, PatchStatus.REJECTED_GUARD) else [],
        created_at=NOW,
    )


def _result(outcome: FixOutcome, patches: list[Patch], winner: Patch | None) -> FixResult:
    return FixResult(
        fix_id=new_id(),
        outcome=outcome,
        message="what happened",
        cluster=CLUSTER,
        patches=patches,
        rejected=[RejectedProposal(3, "edit 1 touches 'scenarios/a.yaml', which is not editable")],
        winner=winner,
        targeted_baseline=RunId(new_id()),
        guard_baseline=None,
        spent_usd=Decimal("0.1234"),
    )


def test_us10_the_report_shows_every_candidate_with_intervals_and_the_winning_diff() -> None:
    accepted = _patch(0, PatchStatus.ACCEPTED)
    bad = _patch(1, PatchStatus.REJECTED_GUARD, regress=True)
    patches = [accepted, bad, _patch(2, PatchStatus.PROPOSED)]

    text = render_markdown(_result(FixOutcome.FIXED, patches, accepted), guard_suite="ci")

    assert text.startswith("# hecklr fix: a validated patch is ready for review\n")
    assert f"**Cluster** {CLUSTER.id}: books without confirming" in text
    assert "**Spent** $0.1234" in text
    assert "| 0 | prompts/system.md | accepted | +50.0 pts [" in text
    assert "| 1 | prompts/system.md | rejected: guard regression |" in text
    assert "| tag=refunds |" in text
    assert "| 2 | prompts/system.md | not evaluated | - | - | - | - | - |" in text
    assert "Paired by trial cell" in text
    assert "- proposal 3: edit 1 touches 'scenarios/a.yaml'" in text
    assert "## Winning patch: candidate 0\n\nRationale 0.\n\n```diff\ndiff --git" in text
    assert "Hecklr never merges it" in text


def test_us10_no_winner_says_so_without_a_diff() -> None:
    text = render_markdown(
        _result(FixOutcome.NO_CANDIDATE_BEAT_BASELINE, [], None), guard_suite="full"
    )

    assert text.startswith("# hecklr fix: no candidate beat the baseline\n")
    assert "```diff" not in text
    assert "(suite `full`)" in text


def test_the_session_is_written_once_with_the_summary_last(tmp_path: Path) -> None:
    accepted = _patch(0, PatchStatus.ACCEPTED)
    result = _result(FixOutcome.FIXED, [accepted], accepted)

    write_session(tmp_path / "s", result, guard_suite="ci", report="# report\n")

    directory = tmp_path / "s"
    assert FailureCluster.model_validate_json((directory / "cluster.json").read_text()) == CLUSTER
    lines = (directory / "patches.jsonl").read_text().splitlines()
    assert [Patch.model_validate_json(line) for line in lines] == [accepted]
    assert (directory / "winner.diff").read_text() == accepted.diff
    assert (directory / "report.md").read_text() == "# report\n"
    summary = FixSummary.model_validate_json((directory / "summary.json").read_text())
    assert summary.outcome is FixOutcome.FIXED
    assert summary.winner_patch_id == accepted.id
    assert summary.rejected == {3: result.rejected[0].reason}
    with pytest.raises(FileExistsError):
        write_session(directory, result, guard_suite="ci", report="")


def test_a_session_without_winner_writes_no_diff(tmp_path: Path) -> None:
    write_session(
        tmp_path,
        _result(FixOutcome.BUDGET_EXHAUSTED, [], None),
        guard_suite="ci",
        report="",
    )

    assert not (tmp_path / "winner.diff").exists()
    summary = FixSummary.model_validate_json((tmp_path / "summary.json").read_text())
    assert summary.winner_candidate is None
