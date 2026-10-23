"""``calibration/judges/review.py``: a person reviews the AI-drafted labels, records their own."""

import importlib.util
import io
import shutil
from collections.abc import Iterator
from pathlib import Path
from types import ModuleType

import pytest
from rich.console import Console

from hecklr.judge_calibration import (
    DRAFT_LABELER,
    CalibrationSet,
    case_trial_id,
    load_set,
    read_labels,
)

SET_DIR = Path(__file__).resolve().parents[2] / "calibration" / "judges"
WORLD = (
    Path(__file__).resolve().parents[2]
    / "examples"
    / "acme-maintenance"
    / "worlds"
    / "acme-maintenance.yaml"
)


@pytest.fixture(scope="module")
def review_script() -> ModuleType:
    spec = importlib.util.spec_from_file_location("calibration_review", SET_DIR / "review.py")
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture
def set_copy(tmp_path: Path) -> CalibrationSet:
    """A copy of the set whose labels file a test may append to."""
    root = tmp_path / "judges"
    shutil.copytree(SET_DIR / "cases", root / "cases")
    shutil.copy(SET_DIR / "labels.jsonl", root / "labels.jsonl")
    (root / "set.yaml").write_text(
        f"version: 1\nworld: {WORLD}\ncases: cases/*.yaml\nlabels: labels.jsonl\n",
        encoding="utf-8",
    )
    return load_set(root)


def _answers(*answers: str) -> Iterator[str]:
    yield from answers


def _run(
    script: ModuleType, calibration_set: CalibrationSet, answers: Iterator[str], **options: object
) -> tuple[int, str]:
    out = io.StringIO()
    reviewed = script.review(
        calibration_set,
        name=options.get("name", "ada"),
        rubrics=options.get("rubrics", ["tone"]),
        show_draft=options.get("show_draft", False),
        ask=lambda question, choices: next(answers),
        console=Console(file=out, width=120),
    )
    return reviewed, out.getvalue()


def test_sc7_review_records_human_labels_next_to_the_drafts(
    review_script: ModuleType, set_copy: CalibrationSet
) -> None:
    before = read_labels(set_copy.labels_path)
    # Agree-with-pass (keeping the draft's rationale), fail with a note, skip, then quit.
    reviewed, output = _run(
        review_script, set_copy, _answers("p", "", "f", "Rude in turn 3.", "s", "q")
    )

    after = read_labels(set_copy.labels_path)
    assert reviewed == 2
    assert after[: len(before)] == before  # append-only
    new = after[len(before) :]
    assert [label.labeler for label in new] == ["human:ada", "human:ada"]
    assert [label.passed for label in new] == [True, False]
    assert new[1].note == "Rude in turn 3."
    assert "Rubric: tone" in output
    assert "AI draft:" in output  # shown after the blind answer


def test_sc7_review_keeps_the_draft_rationale_only_when_the_reviewer_agrees(
    review_script: ModuleType, set_copy: CalibrationSet
) -> None:
    case, _ = next(pair for pair in set_copy.items() if pair[1] == "tone")
    draft = next(
        label
        for label in read_labels(set_copy.labels_path)
        if label.labeler == DRAFT_LABELER
        and label.trial_id == case_trial_id(case.id)
        and label.grader == "judge:tone"
    )
    disagree = "f" if draft.passed else "p"

    _run(review_script, set_copy, _answers(disagree, "", "q"))

    label = read_labels(set_copy.labels_path)[-1]
    assert label.passed is not draft.passed
    assert label.note is None


def test_sc7_review_resumes_where_the_reviewer_stopped(
    review_script: ModuleType, set_copy: CalibrationSet
) -> None:
    _run(review_script, set_copy, _answers("p", "", "q"))
    _, output = _run(review_script, set_copy, _answers("q"))

    total = sum(1 for _, name in set_copy.items() if name == "tone")
    assert f"{total - 1} pair(s) to review as human:ada" in output


def test_sc7_review_can_show_the_draft_first(
    review_script: ModuleType, set_copy: CalibrationSet
) -> None:
    _, output = _run(review_script, set_copy, _answers("q"), show_draft=True)

    assert "AI draft:" in output


def test_review_refuses_a_name_with_spaces(
    review_script: ModuleType, set_copy: CalibrationSet
) -> None:
    with pytest.raises(ValueError, match="single word"):
        _run(review_script, set_copy, _answers(), name="Ada Lovelace")


def test_review_main_parses_the_command_line(
    review_script: ModuleType, set_copy: CalibrationSet, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("rich.prompt.Prompt.ask", lambda *args, **kwargs: "q")

    code = review_script.main(
        ["--name", "ada", "--rubric", "pii_leak", "--set", str(set_copy.root)]
    )

    assert code == 0
