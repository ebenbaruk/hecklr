"""The judge calibration set (SC7): big enough, balanced, valid, labeled by the guide's rules."""

from collections import Counter

import pytest

from hecklr.judge_calibration import (
    CALIBRATED_RUBRICS,
    DRAFT_LABELER,
    HUMAN_PREFIX,
    CalibrationSet,
    case_trial_id,
    grade_context,
    grader_name,
    read_labels,
    rubric,
)
from hecklr_schemas import Channel, HumanLabel

MIN_LABELS_PER_RUBRIC = 40
"""SC7 calibration needs at least 40 labeled conversations per rubric (task WShjudge)."""
PASS_SHARE = (0.40, 0.60)
"""Balanced: neither verdict below 40% of a rubric's labels, so kappa is not dominated by one."""
MIN_BORDERLINE_SHARE = 0.10
"""Hard cases a judge can get wrong for reasons humans would argue about."""


def _per_rubric(labels: list[HumanLabel]) -> dict[str, list[HumanLabel]]:
    grouped: dict[str, list[HumanLabel]] = {name: [] for name in CALIBRATED_RUBRICS}
    for label in labels:
        grouped[label.grader.removeprefix("judge:")].append(label)
    return grouped


def test_sc7_every_builtin_rubric_has_at_least_40_labeled_conversations(
    draft_labels: list[HumanLabel],
) -> None:
    counts = {name: len(rows) for name, rows in _per_rubric(draft_labels).items()}

    assert set(counts) == set(CALIBRATED_RUBRICS)
    assert all(n >= MIN_LABELS_PER_RUBRIC for n in counts.values()), counts


def test_sc7_labels_are_balanced_between_pass_and_fail(draft_labels: list[HumanLabel]) -> None:
    shares = {
        name: sum(label.passed for label in rows) / len(rows)
        for name, rows in _per_rubric(draft_labels).items()
    }

    assert all(PASS_SHARE[0] <= share <= PASS_SHARE[1] for share in shares.values()), shares


def test_sc7_every_rubric_has_hard_borderline_cases(calibration_set: CalibrationSet) -> None:
    labeled = Counter(name for _, name in calibration_set.items())
    borderline = Counter(
        name for case in calibration_set.cases for name, use in case.rubrics.items()
        if use.borderline
    )  # fmt: skip

    for name in CALIBRATED_RUBRICS:
        assert borderline[name] >= MIN_BORDERLINE_SHARE * labeled[name], (name, borderline)


def test_sc7_the_set_spans_chat_voice_and_email(calibration_set: CalibrationSet) -> None:
    channels = Counter(case.channel for case in calibration_set.cases)
    total = len(calibration_set.cases)

    assert set(channels) == set(Channel)
    assert all(count >= 0.10 * total for count in channels.values()), channels


def test_sc7_every_case_replays_against_the_world(calibration_set: CalibrationSet) -> None:
    # Building the trial executes every tool call; a refused call that is not expected raises.
    for case in calibration_set.cases:
        ctx = grade_context(case, calibration_set.world, None, None)
        assert ctx.trial.id == case_trial_id(case.id)
        assert any(event.actor == "agent" for event in ctx.events), case.id


def test_sc7_each_labeled_pair_has_exactly_one_draft_label(
    calibration_set: CalibrationSet, draft_labels: list[HumanLabel]
) -> None:
    expected = {
        (case_trial_id(case.id), grader_name(name)) for case, name in calibration_set.items()
    }
    drafted = Counter((label.trial_id, label.grader) for label in draft_labels)

    assert set(drafted) == expected
    assert set(drafted.values()) == {1}


def test_sc7_labels_pin_the_current_rubric_versions(calibration_set: CalibrationSet) -> None:
    # A rubric edit changes its version; labels of the old wording no longer count (re-review
    # them with calibration/judges/review.py, see GUIDE.md).
    versions = {grader_name(name): rubric(name).version for name in CALIBRATED_RUBRICS}
    stale = [
        (label.grader, label.labeler)
        for label in read_labels(calibration_set.labels_path)
        if label.grader_version != versions[label.grader]
    ]

    assert not stale, f"labels of an outdated rubric version: {Counter(stale)}"


def test_sc7_every_label_has_a_one_line_rationale_and_a_known_labeler(
    calibration_set: CalibrationSet,
) -> None:
    for label in read_labels(calibration_set.labels_path):
        assert label.labeler == DRAFT_LABELER or label.labeler.startswith(HUMAN_PREFIX)
        if label.labeler == DRAFT_LABELER:
            assert label.note, label
            assert "\n" not in label.note, label


@pytest.mark.parametrize("name", CALIBRATED_RUBRICS)
def test_sc7_every_rubric_loads_with_a_stable_version(name: str) -> None:
    assert rubric(name).version == rubric(name).version
    assert len(rubric(name).version) == 12
