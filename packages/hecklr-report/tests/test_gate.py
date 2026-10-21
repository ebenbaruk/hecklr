"""gate_exit_code: 0 pass, 1 regression, 2 inconclusive (configurable), 3 harness error."""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hecklr_report import (
    EXIT_HARNESS_ERROR,
    EXIT_INCONCLUSIVE,
    EXIT_PASS,
    EXIT_REGRESSION,
    Analysis,
    gate_exit_code,
)
from hecklr_schemas import GateConfig, Verdict

if TYPE_CHECKING:
    from hecklr_core import RunResults

FAIL_ON_INCONCLUSIVE = GateConfig()
PASS_ON_INCONCLUSIVE = GateConfig(on_inconclusive="pass")


def with_verdict(analysis: Analysis, verdict: Verdict) -> Analysis:
    suite = analysis.comparisons[0].model_copy(update={"verdict": verdict})
    return analysis.model_copy(
        update={"comparisons": [suite, *analysis.comparisons[1:]], "verdict": verdict}
    )


@pytest.mark.parametrize(
    ("verdict", "gate", "code"),
    [
        (Verdict.REGRESSION, FAIL_ON_INCONCLUSIVE, EXIT_REGRESSION),
        (Verdict.REGRESSION, PASS_ON_INCONCLUSIVE, EXIT_REGRESSION),
        (Verdict.INCONCLUSIVE, FAIL_ON_INCONCLUSIVE, EXIT_INCONCLUSIVE),
        (Verdict.INCONCLUSIVE, PASS_ON_INCONCLUSIVE, EXIT_PASS),
        (Verdict.IMPROVEMENT, FAIL_ON_INCONCLUSIVE, EXIT_PASS),
        (Verdict.NO_CHANGE, FAIL_ON_INCONCLUSIVE, EXIT_PASS),
    ],
)
def test_us04_exit_code_follows_the_suite_verdict(
    regression: tuple[Analysis, RunResults], verdict: Verdict, gate: GateConfig, code: int
) -> None:
    assert gate_exit_code(with_verdict(regression[0], verdict), gate) == code


def test_us04_slice_regressions_alone_do_not_fail_the_gate(
    regression: tuple[Analysis, RunResults],
) -> None:
    analysis = with_verdict(regression[0], Verdict.NO_CHANGE)
    slice_regression = analysis.comparisons[0].model_copy(
        update={"slice": {"tag": "billing"}, "verdict": Verdict.REGRESSION}
    )
    analysis = analysis.model_copy(
        update={"comparisons": [*analysis.comparisons, slice_regression]}
    )

    assert gate_exit_code(analysis, FAIL_ON_INCONCLUSIVE) == EXIT_PASS


def test_problems_exit_3(regression: tuple[Analysis, RunResults]) -> None:
    analysis = regression[0].model_copy(update={"problems": ["candidate run X is failed"]})

    assert gate_exit_code(analysis, FAIL_ON_INCONCLUSIVE) == EXIT_HARNESS_ERROR


def test_no_baseline_comparison_exits_3(regression: tuple[Analysis, RunResults]) -> None:
    analysis = regression[0].model_copy(update={"comparisons": [], "verdict": None})

    assert gate_exit_code(analysis, PASS_ON_INCONCLUSIVE) == EXIT_HARNESS_ERROR
