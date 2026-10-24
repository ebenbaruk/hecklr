from datetime import UTC, datetime, timedelta

import numpy as np
import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_schemas import Grade, GradeId, HumanLabel, IntervalMethod, LabelId, TrialId, new_id
from hecklr_stats.flaky import flaky_scenarios
from hecklr_stats.judge import (
    KAPPA_TRUST_THRESHOLD,
    MIN_LABELS_PER_CLASS,
    calibrate_judge,
    cohen_kappa,
    judge_drift,
    kappa_estimate,
)
from hecklr_stats.outcomes import TrialOutcome

T0 = datetime(2026, 10, 21, tzinfo=UTC)
GRADER = "judge:resolved"


def _grade(
    trial: TrialId,
    passed: bool,
    *,
    model: str = "anthropic/claude-sonnet-5-5",
    version: str = "1",
    status: str = "ok",
) -> Grade:
    return Grade(
        id=GradeId(new_id()),
        trial_id=trial,
        grader=GRADER,
        grader_version=version,
        status=status,  # type: ignore[arg-type]  # tests pass the Literal values as plain str
        value=passed,
        passed=passed and status == "ok",
        judge_model=model,
    )


def _label(
    grade: Grade, passed: bool, *, labeler: str = "ana", minutes: int = 0, version: str = "1"
) -> HumanLabel:
    return HumanLabel(
        id=LabelId(new_id()),
        trial_id=grade.trial_id,
        grade_id=grade.id,
        grader=GRADER,
        grader_version=version,
        passed=passed,
        labeler=labeler,
        created_at=T0 + timedelta(minutes=minutes),
    )


def _labeled_set(judge: list[bool], human: list[bool]) -> tuple[list[Grade], list[HumanLabel]]:
    trials = [TrialId(new_id()) for _ in judge]
    grades = [_grade(t, j) for t, j in zip(trials, judge, strict=True)]
    labels = [_label(g, h) for g, h in zip(grades, human, strict=True)]
    return grades, labels


# --- Cohen's kappa ----------------------------------------------------------------------------


def test_cohen_kappa_matches_the_textbook_example() -> None:
    # 2x2 table: both yes 20, judge-only yes 5, human-only yes 10, both no 15 -> kappa 0.4.
    judge = [True] * 25 + [False] * 25
    human = [True] * 20 + [False] * 5 + [True] * 10 + [False] * 15
    assert cohen_kappa(judge, human) == pytest.approx(0.4)
    assert cohen_kappa([True, True, False, False], [True, False, False, False]) == pytest.approx(
        0.5
    )


def test_cohen_kappa_edge_cases() -> None:
    assert cohen_kappa([True, False], [True, False]) == 1.0
    assert cohen_kappa([True, False], [False, True]) == -1.0
    assert cohen_kappa([True, True], [True, True]) == 1.0  # constant raters never disagree
    assert cohen_kappa([True, True], [False, False]) == 0.0


@given(st.lists(st.tuples(st.booleans(), st.booleans()), min_size=1, max_size=60))
def test_cohen_kappa_is_symmetric_and_bounded(pairs: list[tuple[bool, bool]]) -> None:
    judge = [a for a, _ in pairs]
    human = [b for _, b in pairs]
    kappa = cohen_kappa(judge, human)
    assert -1.0 <= kappa <= 1.0
    assert kappa == pytest.approx(cohen_kappa(human, judge))


def test_kappa_estimate_has_a_bootstrap_interval() -> None:
    rng = np.random.default_rng(0)
    human = rng.random(200) < 0.7
    judge = np.where(rng.random(200) < 0.9, human, ~human)
    estimate = kappa_estimate(judge, human, seed=1, resamples=2_000)
    assert estimate.method is IntervalMethod.BOOTSTRAP
    assert estimate.n == 200
    assert estimate.ci_low < estimate.value < estimate.ci_high
    assert estimate == kappa_estimate(judge, human, seed=1, resamples=2_000)
    with pytest.raises(ValueError, match="same, non-zero"):
        kappa_estimate([True], [True, False], seed=0, resamples=10)


# --- calibration --------------------------------------------------------------------------------


def test_sc7_us11_calibration_reports_kappa_precision_recall() -> None:
    judge = [True] * 25 + [False] * 25
    human = [True] * 20 + [False] * 5 + [True] * 10 + [False] * 15
    grades, labels = _labeled_set(judge, human)
    calibration = calibrate_judge(grades, labels, seed=0, resamples=1_000)
    assert calibration.n == 50
    assert calibration.kappa.value == pytest.approx(0.4)
    assert not calibration.trusted
    # Fail is the positive class: judge failed 25, humans agree on 15; humans failed 20.
    assert calibration.precision is not None
    assert calibration.recall is not None
    assert calibration.precision.value == pytest.approx(15 / 25)
    assert calibration.recall.value == pytest.approx(15 / 20)
    assert calibration.precision.method is IntervalMethod.WILSON
    assert (calibration.grader, calibration.grader_version) == (GRADER, "1")
    assert calibration.judge_model == "anthropic/claude-sonnet-5-5"


def test_sc7_a_judge_that_agrees_is_trusted() -> None:
    rng = np.random.default_rng(2)
    human = (rng.random(100) < 0.6).tolist()
    judge = [h if i % 20 else not h for i, h in enumerate(human)]  # 95% agreement
    grades, labels = _labeled_set(judge, human)
    calibration = calibrate_judge(grades, labels, seed=0, resamples=1_000)
    assert calibration.kappa.value >= KAPPA_TRUST_THRESHOLD
    assert calibration.trusted


@pytest.mark.parametrize("n", [1, 2, 5, 20])
@pytest.mark.parametrize("label", [True, False])
def test_issue100_a_rubber_stamp_judge_on_one_class_labels_is_not_trusted(
    n: int, label: bool
) -> None:
    grades, labels = _labeled_set([label] * n, [label] * n)

    calibration = calibrate_judge(grades, labels, seed=0, resamples=500)

    assert calibration.kappa.value == 1.0  # the raters never disagreed: 0/0, by convention
    assert not calibration.trusted
    passes, fails = (n, 0) if label else (0, n)
    assert calibration.untrusted_reason == (
        f"needs {MIN_LABELS_PER_CLASS}+ human labels of each class, has {passes} pass and "
        f"{fails} fail"
    )


@pytest.mark.parametrize(
    ("fails", "trusted"), [(MIN_LABELS_PER_CLASS - 1, False), (MIN_LABELS_PER_CLASS, True)]
)
def test_issue100_trust_needs_enough_labels_of_each_class(fails: int, trusted: bool) -> None:
    human = [True] * 30 + [False] * fails
    grades, labels = _labeled_set(human, human)  # a judge that agrees on every label

    calibration = calibrate_judge(grades, labels, seed=0, resamples=500)

    assert (calibration.human_passes, calibration.human_fails) == (30, fails)
    assert calibration.kappa.value == 1.0
    assert calibration.trusted is trusted


def test_issue100_low_agreement_says_kappa_is_the_reason() -> None:
    human = [True] * 20 + [False] * 20
    grades, labels = _labeled_set([True] * 40, human)  # always "pass"

    calibration = calibrate_judge(grades, labels, seed=0, resamples=500)

    assert calibration.untrusted_reason == "kappa < 0.70"


def test_calibration_uses_newest_grade_and_label_and_skips_others() -> None:
    trial = TrialId(new_id())
    old_grade = _grade(trial, False)
    new_grade = _grade(trial, True)
    errored = _grade(TrialId(new_id()), False, status="error")
    labels = [
        _label(new_grade, False, minutes=0),
        _label(new_grade, True, minutes=5),  # relabel by the same person wins
        _label(new_grade, False, minutes=1),  # an older label read later still loses
        _label(new_grade, True, labeler="ben"),
        _label(new_grade, False, version="2"),  # another rubric version: ignored
        _label(errored, False),  # its grade is not ok: ignored
    ]
    calibration = calibrate_judge([new_grade, old_grade, errored], labels, seed=0, resamples=100)
    assert calibration.n == 2
    assert calibration.kappa.value == 1.0
    assert calibration.precision is None  # the judge failed nothing
    assert calibration.recall is None  # humans failed nothing


def test_calibration_rejects_mixed_or_unlabeled_grades() -> None:
    trial = TrialId(new_id())
    grades = [_grade(trial, True), _grade(trial, True, model="openai/gpt-4.1")]
    with pytest.raises(ValueError, match="exactly one grader"):
        calibrate_judge(grades, [], seed=0, resamples=10)
    with pytest.raises(ValueError, match="no human labels"):
        calibrate_judge(grades[:1], [], seed=0, resamples=10)


def test_judge_drift_flags_a_worse_model() -> None:
    rng = np.random.default_rng(3)
    human = (rng.random(150) < 0.6).tolist()
    trials = [TrialId(new_id()) for _ in human]
    good = [_grade(t, h) for t, h in zip(trials, human, strict=True)]
    noisy = [
        _grade(t, h if rng.random() < 0.7 else not h, model="openai/gpt-4.1-nano")
        for t, h in zip(trials, human, strict=True)
    ]
    labels = [_label(g, h) for g, h in zip(good, human, strict=True)]
    drift = judge_drift(good, noisy, labels, seed=0, resamples=2_000)
    assert drift.reference.kappa.value == 1.0
    assert drift.candidate.kappa.value < 0.6
    assert drift.delta.value == pytest.approx(
        drift.candidate.kappa.value - drift.reference.kappa.value
    )
    assert drift.delta.ci_high < 0
    assert drift.drifted
    assert drift.candidate.judge_model == "openai/gpt-4.1-nano"
    same = judge_drift(good, good, labels, seed=0, resamples=500)
    assert not same.drifted
    assert same.delta.value == 0.0


def test_judge_drift_needs_the_same_rubric_and_shared_items() -> None:
    first = [_grade(TrialId(new_id()), True)]
    other_version = [_grade(TrialId(new_id()), True, version="2", model="openai/gpt-4.1")]
    with pytest.raises(ValueError, match="same grader and rubric"):
        judge_drift(first, other_version, [], seed=0, resamples=10)
    disjoint = [_grade(TrialId(new_id()), True, model="openai/gpt-4.1")]
    labels = [_label(first[0], True)]
    with pytest.raises(ValueError, match="share no labeled items"):
        judge_drift(first, disjoint, labels, seed=0, resamples=10)


# --- flaky scenarios ------------------------------------------------------------------------------


def _runs(rates: dict[str, float], trials: int) -> list[TrialOutcome]:
    return [
        TrialOutcome(scenario_id=sid, passed=i < round(rate * trials))
        for sid, rate in rates.items()
        for i in range(trials)
    ]


def test_flaky_scenarios_between_20_and_80_percent() -> None:
    outcomes = _runs({"stable": 1.0, "broken": 0.0, "flaky": 0.5, "edge-low": 0.2, "rare": 0.1}, 10)
    flagged = flaky_scenarios(outcomes)
    assert [f.scenario_id for f in flagged] == ["edge-low", "flaky"]
    assert flagged[1].pass_rate.value == 0.5
    assert flagged[1].pass_rate.method is IntervalMethod.WILSON
    assert flagged[1].pass_rate.n == 10


def test_flaky_pools_identical_runs_and_validates_bounds() -> None:
    first = _runs({"s": 1.0}, 5)
    second = _runs({"s": 0.0}, 5)
    (flagged,) = flaky_scenarios(first + second)
    assert flagged.pass_rate.value == 0.5
    with pytest.raises(ValueError, match="low <= high"):
        flaky_scenarios(first, low=0.9, high=0.1)
