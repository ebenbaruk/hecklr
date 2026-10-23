"""The judge calibration set (SC7): big enough, balanced, valid, labeled by the guide's rules."""

from collections import Counter

import pytest

from hecklr.judge_calibration import (
    CALIBRATED_RUBRICS,
    DRAFT_LABELER,
    HUMAN_PREFIX,
    MIN_LABELS_PER_RUBRIC,
    CalibrationSet,
    case_trial_id,
    grade_context,
    grader_name,
    labeled_pairs,
    make_label,
    now,
    rubric,
)
from hecklr_schemas import Channel, HumanLabel

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
    current_labels: list[HumanLabel],
) -> None:
    counts = {name: len(rows) for name, rows in _per_rubric(current_labels).items()}

    assert set(counts) == set(CALIBRATED_RUBRICS)
    assert all(n >= MIN_LABELS_PER_RUBRIC for n in counts.values()), counts


def test_sc7_labels_are_balanced_between_pass_and_fail(current_labels: list[HumanLabel]) -> None:
    shares = {
        name: sum(label.passed for label in rows) / len(rows)
        for name, rows in _per_rubric(current_labels).items()
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


def test_sc7_every_labeled_pair_has_a_label_of_its_current_rubric_version(
    calibration_set: CalibrationSet, all_labels: list[HumanLabel]
) -> None:
    # A rubric edit (or one of the shared judge prompt or transcript format) changes its version.
    # The old labels stay (append-only) but judged other words, so the pair must be labeled again.
    current = labeled_pairs(calibration_set, all_labels)
    changed = sorted(
        {name for case, name in calibration_set.items() if (case.id, name) not in current}
    )

    assert not changed, "".join(
        f"\nrubric {name} changed since it was labeled: re-review it with "
        f"`uv run python calibration/judges/review.py --name <you> --rubric {name}` (GUIDE.md)"
        for name in changed
    )


def _drafts_per_version(labels: list[HumanLabel]) -> set[int]:
    return set(
        Counter(
            (label.trial_id, label.grader, label.grader_version)
            for label in labels
            if label.labeler == DRAFT_LABELER
        ).values()
    )


def test_sc7_labels_belong_to_the_set_with_at_most_one_draft_per_rubric_version(
    calibration_set: CalibrationSet, all_labels: list[HumanLabel]
) -> None:
    pairs = {(case_trial_id(case.id), grader_name(name)) for case, name in calibration_set.items()}

    assert {(label.trial_id, label.grader) for label in all_labels} <= pairs
    assert _drafts_per_version(all_labels) == {1}


def test_sc7_after_a_rubric_change_appending_a_review_restores_the_invariants(
    calibration_set: CalibrationSet, all_labels: list[HumanLabel]
) -> None:
    # The documented recovery (GUIDE.md): rows are never edited or deleted, a review appends.
    aged = [
        label.model_copy(update={"grader_version": "before-the-edit"})
        if label.grader == "judge:tone"
        else label
        for label in all_labels
    ]
    tone_pairs = [(case, name) for case, name in calibration_set.items() if name == "tone"]
    assert {name for _, name in tone_pairs} - {
        name for _, name in labeled_pairs(calibration_set, aged)
    } == {"tone"}

    review = [
        make_label(case, name, passed=True, labeler="human:ada", note=None, created_at=now())
        for case, name in tone_pairs
    ]
    reviewed = aged + review

    assert labeled_pairs(calibration_set, reviewed) == {
        (case.id, name) for case, name in calibration_set.items()
    }
    assert _drafts_per_version(reviewed) == {1}


def test_sc7_every_label_has_a_one_line_rationale_and_a_known_labeler(
    all_labels: list[HumanLabel],
) -> None:
    for label in all_labels:
        assert label.labeler == DRAFT_LABELER or label.labeler.startswith(HUMAN_PREFIX)
        if label.labeler == DRAFT_LABELER:
            assert label.note, label
            assert "\n" not in label.note, label


@pytest.mark.parametrize("name", CALIBRATED_RUBRICS)
def test_sc7_every_rubric_loads_with_a_stable_version(name: str) -> None:
    assert rubric(name).version == rubric(name).version
    assert len(rubric(name).version) == 12
