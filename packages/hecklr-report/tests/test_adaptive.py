"""Issue #103: an adaptive run is compared at one group-sequential look per batch of trials."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from rich.console import Console

from hecklr_core import extend_matrix, make_spec
from hecklr_report import analyze, render_html, render_markdown, render_terminal
from hecklr_report.analysis import candidate_looks
from hecklr_report.outcomes import convert
from hecklr_schemas import Channel, Scenario, Verdict
from hecklr_stats import group_sequential_boundaries

if TYPE_CHECKING:
    from pathlib import Path

    from conftest import Kit, RunBuilder
    from hecklr_core import RunResults

SCENARIOS = [f"s{i:02d}" for i in range(20)]
FIRST_PASS = 4
EXTRA = 2
PERSONA = "busy-homeowner"


def _baseline(kit: type[Kit]) -> RunResults:
    builder = kit.RunBuilder(seed=1)
    for sid in SCENARIOS:
        for _ in range(FIRST_PASS):
            builder.trial(sid)
    return builder.build()


def _adaptive(kit: type[Kit], failing: int, *, extra: int = EXTRA) -> RunBuilder:
    """A candidate whose first ``failing`` scenarios fail every trial, with an adaptive pass.

    The matrix is laid out the way ``hecklr_core`` plans it: the first pass cell by cell, then
    the extra cells ``extend_matrix`` appends.
    """
    builder = kit.RunBuilder(seed=2)
    seed = builder.run.seed
    first = [
        make_spec(seed, sid, PERSONA, Channel.CHAT, index)
        for sid in SCENARIOS
        for index in range(FIRST_PASS)
    ]
    extended = [*first, *extend_matrix(first, seed, SCENARIOS, extra)] if extra else first
    builder.run = builder.run.model_copy(update={"matrix": extended, "adaptive": bool(extra)})
    for position, sid in enumerate(SCENARIOS):
        for _ in range(FIRST_PASS + extra):
            builder.trial(sid, passed=position >= failing)
    return builder


def test_issue103_looks_split_the_first_pass_from_the_adaptive_pass(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    results = _adaptive(kit, failing=0).build()

    looks = candidate_looks(results, convert(results, scenarios, {}))

    assert [len(look) for look in looks] == [80, 120]


def test_issue103_a_run_without_an_adaptive_pass_has_one_look(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    results = _adaptive(kit, failing=0, extra=0).build()

    looks = candidate_looks(results, convert(results, scenarios, {}))

    assert [len(look) for look in looks] == [80]


def test_issue103_a_first_pass_with_nothing_analyzable_is_not_a_look(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    builder = _adaptive(kit, failing=0)
    adaptive = builder.run
    builder = kit.RunBuilder(seed=2)
    builder.run = adaptive
    for sid in SCENARIOS:
        for _ in range(FIRST_PASS):
            builder.trial(sid, status="harness_error")
        for _ in range(EXTRA):
            builder.trial(sid)
    results = builder.build()

    looks = candidate_looks(results, convert(results, scenarios, {}))

    assert [len(look) for look in looks] == [40]


def test_issue103_an_adaptive_run_is_decided_at_the_first_look_that_crosses(
    kit: type[Kit], scenarios: dict[str, Scenario], tmp_path: Path
) -> None:
    first, _ = group_sequential_boundaries([2 / 3, 1.0], kit.stats.alpha)
    results = _adaptive(kit, failing=10).build()

    analysis = analyze(results, _baseline(kit), kit.stats, scenarios)

    suite = analysis.comparisons[0]
    assert analysis.verdict is Verdict.REGRESSION
    assert suite.look == 1
    assert suite.alpha_spent == pytest.approx(first.alpha_spent)
    assert suite.n_trials_candidate == 80
    shown = f"adaptive look 1, \N{GREEK SMALL LETTER ALPHA} spent {first.alpha_spent:.3f}"
    assert shown in render_markdown(analysis)
    console = Console(record=True, width=240, color_system=None)
    render_terminal(analysis, console)
    assert shown in console.export_text()
    assert shown in render_html(analysis, results, tmp_path / "r.html").read_text("utf-8")


def test_issue103_an_adaptive_run_below_the_first_boundary_goes_to_the_next_look(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    # 7 failing scenarios: p ~ 0.016 is over the first look's ~0.012 but under the last one's.
    analysis = analyze(_adaptive(kit, failing=7).build(), _baseline(kit), kit.stats, scenarios)

    suite = analysis.comparisons[0]
    assert analysis.verdict is Verdict.REGRESSION
    assert suite.look == 2
    assert suite.alpha_spent == pytest.approx(kit.stats.alpha)
    assert suite.n_trials_candidate == 120


def test_issue103_a_fixed_run_keeps_the_fixed_design(
    kit: type[Kit], scenarios: dict[str, Scenario]
) -> None:
    analysis = analyze(
        _adaptive(kit, failing=10, extra=0).build(), _baseline(kit), kit.stats, scenarios
    )

    suite = analysis.comparisons[0]
    assert suite.look == 1
    assert suite.alpha_spent is None
    assert "adaptive look" not in render_markdown(analysis)
