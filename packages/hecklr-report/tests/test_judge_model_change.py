"""Issue #102: a judge model change between baseline and candidate is a drift warning."""

from __future__ import annotations

from typing import TYPE_CHECKING

from rich.console import Console

from hecklr_report import analyze, render_html, render_markdown, render_terminal

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Kit, RunBuilder
    from hecklr_report import Analysis
    from hecklr_schemas import Scenario

WARNING = "re-score the labeled set: hecklr calibrate --judges --set DIR --drift-from fake/judge"


def _run(kit: type[Kit], seed: int, judge_model: str | None) -> RunBuilder:
    builder = kit.RunBuilder(seed=seed)
    builder.run = builder.run.model_copy(update={"judge_model": judge_model})
    for _ in range(3):
        builder.trial("s00")
    return builder


def _analysis(
    kit: type[Kit], scenarios: dict[str, Scenario], before: str | None, after: str | None
) -> Analysis:
    baseline = _run(kit, 1, before).build()
    return analyze(_run(kit, 2, after).build(), baseline, kit.stats, scenarios)


def test_issue102_spec_changing_the_judge_model_warns_in_every_report(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    candidate = _run(kit, 2, "fake/judge-2").build()
    analysis = analyze(candidate, _run(kit, 1, "fake/judge").build(), kit.stats, scenarios)

    assert analysis.judge_model_change == ("fake/judge", "fake/judge-2")
    comment = render_markdown(analysis)
    assert "**Judge model changed** `fake/judge` → `fake/judge-2`" in comment
    assert WARNING in comment
    console = Console(record=True, width=240, color_system=None)
    render_terminal(analysis, console)
    text = console.export_text()
    assert "Judge model changed: fake/judge → fake/judge-2" in text
    assert WARNING in text
    page = render_html(analysis, candidate, tmp_path / "r.html").read_text("utf-8")
    assert "Judge model changed" in page
    assert "--drift-from fake/judge" in page


def test_issue102_the_same_or_an_unknown_judge_model_is_no_change(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    for before, after in [("fake/judge", "fake/judge"), (None, "fake/judge"), ("fake/judge", None)]:
        analysis = _analysis(kit, scenarios, before, after)

        assert analysis.judge_model_change is None
        assert "Judge model changed" not in render_markdown(analysis)


def test_issue102_a_single_run_has_no_judge_model_change(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    analysis = analyze(_run(kit, 1, "fake/judge").build(), None, kit.stats, scenarios)

    assert analysis.judge_model_change is None
