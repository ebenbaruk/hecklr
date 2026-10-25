"""Judge labeling (spec, "Judge calibration"; US-11, SC7): queue, human labels, agreement.

Labels are appended to ``<output_dir>/labels.jsonl``, one ``HumanLabel`` a line: the file
``hecklr calibrate --judges --labels`` reads. The newest label per (grade, labeler) wins.
"""

import random
import threading
from collections import defaultdict
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from itertools import zip_longest
from pathlib import Path
from typing import Final

from pydantic import ValidationError

from hecklr.ui_api.jsonl import append_line
from hecklr.ui_api.models import Agreement, AgreementReport, QueueItem
from hecklr_core import RunResults
from hecklr_schemas import Grade, HumanLabel, LabelId, new_id
from hecklr_stats import calibrate_judge

LABELS_FILE: Final = "labels.jsonl"
TRANSCRIPT_KINDS: Final = ("utterance", "email")


class LabelFileError(ValueError):
    """A line of the labels file is not a ``HumanLabel``."""


class UnknownGradeError(LookupError):
    """The run holds no judge grade with this id."""


class LabelBook:
    """The project's human labels: read from and appended to one JSONL file."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.Lock()

    def all(self) -> list[HumanLabel]:
        """Every label, in file order.

        Raises:
            LabelFileError: a line is not a valid label (the file was edited by hand).
        """
        if not self.path.is_file():
            return []
        labels = []
        with self.path.open(encoding="utf-8") as lines:
            for number, line in enumerate(lines, start=1):
                if not line.strip():
                    continue
                try:
                    labels.append(HumanLabel.model_validate_json(line))
                except ValidationError as exc:
                    raise LabelFileError(f"{self.path}:{number}: not a human label") from exc
        return labels

    def add(
        self, grade: Grade, *, passed: bool, labeler: str, note: str | None, now: datetime
    ) -> HumanLabel:
        """Append a label of ``grade`` and return it."""
        label = HumanLabel(
            id=LabelId(new_id()),
            trial_id=grade.trial_id,
            grade_id=grade.id,
            grader=grade.grader,
            grader_version=grade.grader_version,
            passed=passed,
            labeler=labeler,
            note=note or None,
            created_at=now,
        )
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            append_line(self.path, label.model_dump_json())
        return label


def newest_by_grade(labels: Iterable[HumanLabel], labeler: str) -> dict[str, HumanLabel]:
    """``labeler``'s newest label of each grade, by grade id."""
    newest: dict[str, HumanLabel] = {}
    for label in labels:
        if label.labeler != labeler:
            continue
        current = newest.get(label.grade_id)
        if current is None or label.created_at >= current.created_at:
            newest[label.grade_id] = label
    return newest


def judge_grades(results: RunResults) -> list[Grade]:
    """The run's ``ok`` judge grades: the ones a human can agree or disagree with.

    A grade is a judge's when a judge model decided it, whatever the grader is called: the safety
    graders ``injection_resisted``, ``no_policy_override`` and ``no_pii_leak`` ask a judge but
    report under their own name, and their deterministic pre-checks carry no judge model.
    """
    return [g for g in results.grades if g.judge_model is not None and g.status == "ok"]


def find_judge_grade(results: RunResults, grade_id: str) -> Grade:
    """The judge grade ``grade_id`` of the run.

    Raises:
        UnknownGradeError: no such ``ok`` judge grade.
    """
    for grade in judge_grades(results):
        if grade.id == grade_id:
            return grade
    raise UnknownGradeError(f"run {results.run.id} has no judge grade {grade_id!r}")


def label_queue(
    runs: Sequence[RunResults],
    titles: dict[str, str],
    labels: Sequence[HumanLabel],
    *,
    labeler: str,
    grader: str | None,
    limit: int,
    seed: int,
) -> tuple[list[QueueItem], int]:
    """A stratified sample of judge grades, unlabeled first, and how many are labeled.

    Each rubric's grades are shuffled with ``seed`` and the rubrics interleaved round-robin, so
    any prefix of the queue covers every rubric about equally (spec: "a stratified sample of
    judged trials per rubric").
    """
    mine = newest_by_grade(labels, labeler)
    by_rubric: dict[str, list[tuple[RunResults, Grade]]] = defaultdict(list)
    for results in runs:
        for grade in judge_grades(results):
            if grader is None or grade.grader == grader:
                by_rubric[grade.grader].append((results, grade))
    rng = random.Random(seed)  # noqa: S311 - a seeded, reproducible sample, not a secret
    strata = []
    for rubric in sorted(by_rubric):
        items = sorted(by_rubric[rubric], key=lambda item: item[1].id)
        rng.shuffle(items)
        strata.append(items)
    ordered = [item for row in zip_longest(*strata) for item in row if item is not None]
    sample = sorted(ordered[:limit], key=lambda item: item[1].id in mine)
    queue = [_item(results, grade, titles, mine.get(grade.id)) for results, grade in sample]
    return queue, sum(item.label is not None for item in queue)


def _item(
    results: RunResults, grade: Grade, titles: dict[str, str], label: HumanLabel | None
) -> QueueItem:
    trial = next(t for t in results.trials if t.id == grade.trial_id)
    transcript = sorted(
        (e for e in results.events_of(trial) if e.kind in TRANSCRIPT_KINDS),
        key=lambda e: (e.t_ms, e.seq),
    )
    return QueueItem(
        run_id=results.run.id,
        trial_id=trial.id,
        scenario_id=trial.scenario_id,
        title=titles.get(trial.scenario_id, trial.scenario_id),
        grade=grade,
        transcript=transcript,
        label=label,
    )


def agreement(
    grades: Sequence[Grade], labels: Sequence[HumanLabel], *, seed: int, resamples: int
) -> AgreementReport:
    """Cohen's κ, precision and recall of every labeled judge, each with its interval.

    Grouped like ``hecklr calibrate --judges``: per (rubric, rubric version, judge model), so the
    UI and the CLI report the same numbers for the same labels and seed.
    """
    groups: dict[tuple[str, str, str | None], list[Grade]] = defaultdict(list)
    for grade in grades:
        groups[(grade.grader, grade.grader_version, grade.judge_model)].append(grade)
    judges = []
    matched: set[str] = set()
    for key in sorted(groups, key=lambda k: (k[0], k[1], k[2] or "")):
        grader, version, _ = key
        trials = {grade.trial_id for grade in groups[key]}
        mine = [
            label
            for label in labels
            if (label.grader, label.grader_version) == (grader, version)
            and label.trial_id in trials
        ]
        if not mine:
            continue
        matched.update(label.id for label in mine)
        calibration = calibrate_judge(groups[key], mine, seed=seed, resamples=resamples)
        judges.append(
            Agreement(
                grader=calibration.grader,
                grader_version=calibration.grader_version,
                judge_model=calibration.judge_model,
                n=calibration.n,
                kappa=calibration.kappa,
                precision=calibration.precision,
                recall=calibration.recall,
                trusted=calibration.trusted,
            )
        )
    return AgreementReport(labels=len(labels), judges=judges, unmatched=len(labels) - len(matched))


def utc_now() -> datetime:
    """The wall-clock time labels and decisions are stamped with (metadata only)."""
    return datetime.now(UTC)
