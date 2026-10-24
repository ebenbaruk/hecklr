"""Issue #102: scenarios flaky across runs declared identical are flagged in every report."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

from hecklr_report import analyze, render_html, render_markdown, render_terminal
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


def test_issue102_a_scenario_between_20_and_80_percent_across_identical_runs_is_flaky(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    candidate = _run(kit, 2, flaky_passes=2).build()

    baseline = _run(kit, 1, flaky_passes=1).build()

    analysis = analyze(candidate, baseline, kit.stats, scenarios, identical_runs=True)

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


def test_issue102_runs_sharing_a_commit_are_not_assumed_identical(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    # Edit the agent without committing and re-run: both runs record the same HEAD. A real,
    # deterministic regression (s00 3/3 -> 0/3) must not be pooled into a 50% "flaky" label.
    candidate = _run(kit, 2, flaky_passes=0).build()
    baseline = _run(kit, 1, flaky_passes=3).build()
    assert candidate.run.git_sha == baseline.run.git_sha

    analysis = analyze(candidate, baseline, kit.stats, scenarios)

    assert not analysis.identical_runs
    assert all(s.flaky is None for s in analysis.scenarios)
    assert [s.scenario_id for s in analysis.scenarios if s.flip is not None] == ["s00"]
    assert "Flaky" not in render_markdown(analysis)
    console = Console(record=True, width=240, color_system=None)
    render_terminal(analysis, console)
    assert "Flaky" not in console.export_text()


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

    comment = render_markdown(analyze(runs[1], runs[0], kit.stats, scenarios, identical_runs=True))

    line = next(x for x in comment.splitlines() if x.startswith("**Flaky"))
    assert line.count("`s0") == 4
    assert line.endswith("+2 more")


def test_issue102_the_terminal_lists_a_few_flaky_scenarios_and_counts_the_rest(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    runs = []
    for seed in (1, 2):
        builder = kit.RunBuilder(seed=seed)
        for s in range(6):
            builder.trial(f"s{s:02d}", passed=True)
            builder.trial(f"s{s:02d}", passed=False)
        runs.append(builder.build())
    console = Console(record=True, width=400, color_system=None)

    render_terminal(analyze(runs[1], runs[0], kit.stats, scenarios, identical_runs=True), console)

    line = next(x for x in console.export_text().splitlines() if x.startswith("Flaky"))
    assert line.count(" s0") == 4
    assert line.endswith("+2 more")
