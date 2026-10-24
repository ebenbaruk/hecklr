import pytest

from hecklr_fix.errors import FixError
from hecklr_fix.validation import cell_outcomes, compare_cells, improved, regressions
from hecklr_schemas import Verdict

from .factories import FAST_STATS, make_run, scenario, suite_run

BOOK = [scenario("book-a", ["booking"]), scenario("book-b", ["booking"])]


def test_outcomes_are_keyed_by_trial_cell_and_sliced_by_tags_only() -> None:
    run = suite_run(make_run({"book-a": [True, False, None]}), BOOK)

    outcomes = cell_outcomes(run)

    assert [(o.scenario_id, o.passed) for o in outcomes] == [
        ("book-a|busy-homeowner|chat|0", True),
        ("book-a|busy-homeowner|chat|1", False),
    ]
    assert all(o.tags == {"booking"} and not o.labels for o in outcomes)


def test_targeted_two_scenarios_x_five_trials_fixed_is_a_significant_improvement() -> None:
    """Scenario pairing could never decide on 2 scenarios; cell pairing can (ADR 0015a)."""
    before = suite_run(make_run({"book-a": [False] * 5, "book-b": [False] * 5}), BOOK)
    after = suite_run(make_run({"book-a": [True] * 5, "book-b": [True] * 5}), BOOK)

    comparisons = compare_cells(before, after, stats=FAST_STATS, seed=1)

    assert comparisons[0].slice == {}
    assert comparisons[0].verdict is Verdict.IMPROVEMENT
    assert comparisons[0].p_value < 0.01
    assert improved(comparisons)
    assert not regressions(comparisons)


def test_targeted_no_change_is_not_kept() -> None:
    same = {"book-a": [False, True, False], "book-b": [True, False, True]}
    comparisons = compare_cells(
        suite_run(make_run(same), BOOK), suite_run(make_run(same), BOOK), stats=FAST_STATS, seed=1
    )

    assert not improved(comparisons)


def test_guard_a_regression_on_one_tag_is_caught_even_when_the_suite_improves() -> None:
    scenarios = [scenario(f"book-{i}", ["booking"]) for i in range(4)] + [
        scenario(f"refund-{i}", ["refunds"]) for i in range(3)
    ]
    before = {
        **{f"book-{i}": [False] * 3 for i in range(4)},
        **{f"refund-{i}": [True] * 3 for i in range(3)},
    }
    after = {
        **{f"book-{i}": [True] * 3 for i in range(4)},
        **{f"refund-{i}": [False] * 3 for i in range(3)},
    }

    comparisons = compare_cells(
        suite_run(make_run(before), scenarios),
        suite_run(make_run(after), scenarios),
        stats=FAST_STATS,
        seed=1,
    )

    assert comparisons[0].verdict is not Verdict.REGRESSION
    assert [c.slice for c in regressions(comparisons)] == [{"tag": "refunds"}]


def test_runs_with_nothing_in_common_cannot_be_compared() -> None:
    harness_errors = suite_run(make_run({"book-a": [None]}), BOOK)
    ok = suite_run(make_run({"book-a": [True]}), BOOK)

    with pytest.raises(FixError, match="cannot compare"):
        compare_cells(harness_errors, ok, stats=FAST_STATS, seed=1)


def test_issue99_guard_counts_cells_the_patch_left_without_a_verdict_as_failures() -> None:
    scenarios = [scenario(f"book-{i}", ["booking"]) for i in range(4)] + [
        scenario(f"refund-{i}", ["refunds"]) for i in range(3)
    ]
    before: dict[str, list[bool | None]] = {name.id: [True] * 3 for name in scenarios}
    # The patch makes every refund conversation ungradeable (None: no verdict).
    after: dict[str, list[bool | None]] = {
        **before,
        **{f"refund-{i}": [None] * 3 for i in range(3)},
    }
    baseline = suite_run(make_run(before), scenarios)
    candidate = suite_run(make_run(after), scenarios)

    targeted = compare_cells(baseline, candidate, stats=FAST_STATS, seed=1)
    guard = compare_cells(baseline, candidate, stats=FAST_STATS, seed=1, guard=True)

    assert not regressions(targeted)  # the lost cells simply leave a plain comparison
    assert [c.slice for c in regressions(guard)] == [{}, {"tag": "refunds"}]
    assert guard[0].n_scenarios == 21


def test_issue99_guard_still_excludes_cells_the_baseline_could_not_grade() -> None:
    before: dict[str, list[bool | None]] = {"book-a": [None, True], "book-b": [True, True]}
    after = dict(before)

    (suite, *_) = compare_cells(
        suite_run(make_run(before), BOOK),
        suite_run(make_run(after), BOOK),
        stats=FAST_STATS,
        seed=1,
        guard=True,
    )

    assert suite.n_scenarios == 3
    assert suite.verdict is not Verdict.REGRESSION
