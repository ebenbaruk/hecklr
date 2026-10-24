"""Issue #102: scenarios flaky across identical runs are flagged in every report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

from hecklr_report import analyze, render_html, render_markdown, render_terminal
from hecklr_report.analysis import is_identical
from hecklr_schemas import Scenario
from hecklr_stats import wilson_estimate

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Kit, RunBuilder

SHA = "a" * 40


def _run(kit: type[Kit], seed: int, *, flaky_passes: int, sha: str | None = SHA) -> RunBuilder:
    """``s00`` passes ``flaky_passes`` of 3 trials, ``s01`` all 3."""
    builder = kit.RunBuilder(seed=seed)
    builder.run = builder.run.model_copy(update={"git_sha": sha})
    for i in range(3):
        builder.trial("s00", passed=i < flaky_passes)
        builder.trial("s01")
    return builder


def test_issue102_identical_runs_share_suite_agent_and_commit(kit: type[Kit]) -> None:
    a, b = _run(kit, 1, flaky_passes=1).run, _run(kit, 2, flaky_passes=1).run

    assert is_identical(a, b)
    assert not is_identical(a, b.model_copy(update={"git_sha": "b" * 40}))
    assert not is_identical(a, b.model_copy(update={"suite": "nightly"}))
    assert not is_identical(a.model_copy(update={"git_sha": None}), b)
    agent = b.agent.model_copy(update={"version": "v3"})
    assert not is_identical(a, b.model_copy(update={"agent": agent}))


def test_issue102_a_scenario_between_20_and_80_percent_across_identical_runs_is_flaky(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    candidate = _run(kit, 2, flaky_passes=2).build()

    analysis = analyze(candidate, _run(kit, 1, flaky_passes=1).build(), kit.stats, scenarios)

    flaky = {s.scenario_id: s.flaky for s in analysis.scenarios}
    assert analysis.identical_runs
    assert flaky == {"s00": wilson_estimate(3, 6), "s01": None}
    comment = render_markdown(analysis)
    assert "**Flaky across identical runs** (pass rate 20-80%): `s00` 50.0% [" in comment
    console = Console(record=True, width=240, color_system=None)
    render_terminal(analysis, console)
    assert "Flaky across identical runs (pass rate 20-80%): s00 50.0% [" in console.export_text()
    page = render_html(analysis, candidate, tmp_path / "r.html").read_text("utf-8")
    assert "flaky across identical runs: 50.0% [" in page


def test_issue102_runs_of_different_commits_are_not_checked_for_flakiness(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = _run(kit, 2, flaky_passes=2, sha="b" * 40).build()

    analysis = analyze(candidate, _run(kit, 1, flaky_passes=1).build(), kit.stats, scenarios)

    assert not analysis.identical_runs
    assert all(s.flaky is None for s in analysis.scenarios)
    assert "Flaky" not in render_markdown(analysis)


def test_issue102_the_comment_lists_a_few_flaky_scenarios_and_counts_the_rest(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    runs = []
    for seed in (1, 2):
        builder = kit.RunBuilder(seed=seed)
        builder.run = builder.run.model_copy(update={"git_sha": SHA})
        for s in range(6):
            builder.trial(f"s{s:02d}", passed=True)
            builder.trial(f"s{s:02d}", passed=False)
        runs.append(builder.build())

    comment = render_markdown(analyze(runs[1], runs[0], kit.stats, scenarios))

    line = next(x for x in comment.splitlines() if x.startswith("**Flaky"))
    assert line.count("`s0") == 4
    assert line.endswith("+2 more")


def test_issue102_the_caller_can_declare_runs_identical_without_a_commit(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    candidate = _run(kit, 2, flaky_passes=2, sha=None).build()
    baseline = _run(kit, 1, flaky_passes=1, sha=None).build()

    assert not analyze(candidate, baseline, kit.stats, scenarios).identical_runs
    declared = analyze(candidate, baseline, kit.stats, scenarios, identical_runs=True)

    assert declared.identical_runs
    assert [s.scenario_id for s in declared.scenarios if s.flaky is not None] == ["s00"]
