"""Judge calibration against human labels (spec, "Judge calibration", SC7, US-11).

A label and a grade are about the same judgment when they share trial, grader and rubric version
(``grader_version``). Joining on those, not on the grade id, lets a re-score of the labeled set by a
new judge model be compared with the same human labels (the drift check). Each (trial, labeler)
pair is one observation; the newest label of a labeler wins.

Precision and recall treat **fail** as the positive class: a judge exists to catch failures, so
precision is "when the judge says fail, a human agrees" and recall is "of the failures humans see,
the judge catches this share".
"""

from collections.abc import Sequence
from dataclasses import dataclass

import numpy as np
import numpy.typing as npt

from hecklr_schemas import Estimate, Grade, HumanLabel, IntervalMethod
from hecklr_stats.intervals import (
    DEFAULT_CONFIDENCE,
    chunk_sizes,
    percentile_interval,
    wilson_estimate,
)
from hecklr_stats.outcomes import FloatArray

KAPPA_TRUST_THRESHOLD = 0.70
"""Rubrics whose judge agrees with humans less than this are marked untrusted (SC7)."""

BoolArray = npt.NDArray[np.bool_]
_ItemKey = tuple[str, str]
"""(trial id, labeler): one human judgment of one trial."""


@dataclass(frozen=True, slots=True)
class JudgeCalibration:
    """Agreement of one judge (rubric version and model) with human labels.

    Attributes:
        grader: Rubric name, e.g. ``judge:resolved``.
        grader_version: Rubric version the labels pin.
        judge_model: Model that produced the grades, if any.
        n: Labeled judgments compared.
        kappa: Cohen's kappa with a bootstrap interval.
        precision: Of trials the judge failed, the share humans also failed; ``None`` if the judge
            failed none.
        recall: Of trials humans failed, the share the judge failed; ``None`` if humans failed none.
    """

    grader: str
    grader_version: str
    judge_model: str | None
    n: int
    kappa: Estimate
    precision: Estimate | None
    recall: Estimate | None

    @property
    def trusted(self) -> bool:
        """Whether agreement reaches ``KAPPA_TRUST_THRESHOLD``."""
        return self.kappa.value >= KAPPA_TRUST_THRESHOLD


@dataclass(frozen=True, slots=True)
class JudgeDrift:
    """Agreement with humans of a reference and a candidate judge model on the same labels.

    Attributes:
        reference: Calibration of the judge in use.
        candidate: Calibration of the new judge model.
        delta: Candidate minus reference kappa, paired bootstrap over shared labeled items.
    """

    reference: JudgeCalibration
    candidate: JudgeCalibration
    delta: Estimate

    @property
    def drifted(self) -> bool:
        """Agreement dropped: significantly, or from trusted to untrusted."""
        crossed = self.reference.trusted and not self.candidate.trusted
        return self.delta.ci_high < 0 or crossed


def cohen_kappa(judge: npt.ArrayLike, human: npt.ArrayLike) -> float:
    """Cohen's kappa of two pass/fail raters.

    When both raters give one identical label to everything, chance agreement is 1 and kappa is
    0/0; it is defined as 1 there, since the raters never disagree.
    """
    return float(
        _kappa(np.asarray(judge, dtype=bool)[None, :], np.asarray(human, dtype=bool)[None, :])[0]
    )


def _kappa(judge: BoolArray, human: BoolArray) -> FloatArray:
    """Row-wise kappa of (replicates, items) arrays."""
    observed = np.mean(judge == human, axis=1)
    judge_rate = np.mean(judge, axis=1)
    human_rate = np.mean(human, axis=1)
    chance = judge_rate * human_rate + (1 - judge_rate) * (1 - human_rate)
    denominator = 1 - chance
    kappa = np.ones_like(observed)
    np.divide(observed - chance, denominator, out=kappa, where=denominator > 0)
    return kappa


def kappa_estimate(
    judge: npt.ArrayLike,
    human: npt.ArrayLike,
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> Estimate:
    """Cohen's kappa with a percentile bootstrap interval over labeled items."""
    judge_array = np.asarray(judge, dtype=bool)
    human_array = np.asarray(human, dtype=bool)
    if judge_array.shape != human_array.shape or judge_array.size == 0:
        raise ValueError("need the same, non-zero number of judge and human labels")
    rng = np.random.default_rng(seed)
    replicates = _bootstrap_kappa(judge_array, human_array, rng, resamples)
    return _estimate(
        cohen_kappa(judge_array, human_array), replicates, judge_array.size, confidence
    )


def _bootstrap_kappa(
    judge: BoolArray, human: BoolArray, rng: np.random.Generator, resamples: int
) -> FloatArray:
    parts = []
    for size in chunk_sizes(resamples, judge.size):
        drawn = rng.integers(0, judge.size, size=(size, judge.size))
        parts.append(_kappa(judge[drawn], human[drawn]))
    return np.concatenate(parts)


def _estimate(point: float, replicates: FloatArray, n: int, confidence: float) -> Estimate:
    low, high = percentile_interval(replicates, confidence)
    return Estimate(
        value=point,
        ci_low=min(low, point),
        ci_high=max(high, point),
        confidence=confidence,
        n=n,
        method=IntervalMethod.BOOTSTRAP,
    )


def calibrate_judge(
    grades: Sequence[Grade],
    labels: Sequence[HumanLabel],
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> JudgeCalibration:
    """Agreement of one judge's grades with the human labels of the same rubric version.

    Args:
        grades: Grades of a single grader, rubric version and judge model. Only ``ok`` grades
            count; when a trial was graded more than once, the newest grade (highest ULID) wins.
        labels: Human labels; those of other graders or rubric versions are ignored.
        seed: Bootstrap seed.
        resamples: Bootstrap replicates.
        confidence: Interval coverage.
    """
    grader, version, model = _identity(grades)
    items = _join(grades, labels)
    if not items:
        raise ValueError(f"no human labels match {grader} {version}")
    judge, human = _arrays(items, sorted(items))
    return _calibration(grader, version, model, judge, human, seed, resamples, confidence)


def judge_drift(
    reference: Sequence[Grade],
    candidate: Sequence[Grade],
    labels: Sequence[HumanLabel],
    *,
    seed: int,
    resamples: int,
    confidence: float = DEFAULT_CONFIDENCE,
) -> JudgeDrift:
    """Compare two judge models' agreement with humans on the labeled items both graded.

    Args:
        reference: Grades of the judge model in use.
        candidate: Grades of the same rubric version by the new judge model (the re-score).
        labels: Human labels of that rubric version.
        seed: Bootstrap seed.
        resamples: Bootstrap replicates.
        confidence: Interval coverage.
    """
    ref_grader, ref_version, ref_model = _identity(reference)
    cand_grader, cand_version, cand_model = _identity(candidate)
    if (ref_grader, ref_version) != (cand_grader, cand_version):
        raise ValueError("drift compares judge models on the same grader and rubric version")
    ref_items, cand_items = _join(reference, labels), _join(candidate, labels)
    shared = sorted(ref_items.keys() & cand_items.keys())
    if not shared:
        raise ValueError("the two judges share no labeled items")
    ref_judge, human = _arrays(ref_items, shared)
    cand_judge, _ = _arrays(cand_items, shared)
    rng = np.random.default_rng(seed)
    deltas = []
    for size in chunk_sizes(resamples, len(shared)):
        drawn = rng.integers(0, len(shared), size=(size, len(shared)))
        deltas.append(
            _kappa(cand_judge[drawn], human[drawn]) - _kappa(ref_judge[drawn], human[drawn])
        )
    point = cohen_kappa(cand_judge, human) - cohen_kappa(ref_judge, human)
    return JudgeDrift(
        reference=_calibration(
            ref_grader, ref_version, ref_model, ref_judge, human, seed, resamples, confidence
        ),
        candidate=_calibration(
            cand_grader, cand_version, cand_model, cand_judge, human, seed, resamples, confidence
        ),
        delta=_estimate(point, np.concatenate(deltas), len(shared), confidence),
    )


def _calibration(
    grader: str,
    version: str,
    model: str | None,
    judge: BoolArray,
    human: BoolArray,
    seed: int,
    resamples: int,
    confidence: float,
) -> JudgeCalibration:
    judge_fails, human_fails = ~judge, ~human
    both = int(np.sum(judge_fails & human_fails))
    return JudgeCalibration(
        grader=grader,
        grader_version=version,
        judge_model=model,
        n=int(judge.size),
        kappa=kappa_estimate(judge, human, seed=seed, resamples=resamples, confidence=confidence),
        precision=_share(both, int(judge_fails.sum()), confidence),
        recall=_share(both, int(human_fails.sum()), confidence),
    )


def _share(hits: int, total: int, confidence: float) -> Estimate | None:
    return wilson_estimate(hits, total, confidence) if total else None


def _identity(grades: Sequence[Grade]) -> tuple[str, str, str | None]:
    identities = {(g.grader, g.grader_version, g.judge_model) for g in grades}
    if len(identities) != 1:
        raise ValueError(
            "grades must come from exactly one grader, rubric version and judge model, "
            f"got {sorted(identities, key=str)}"
        )
    return identities.pop()


def _join(
    grades: Sequence[Grade], labels: Sequence[HumanLabel]
) -> dict[_ItemKey, tuple[bool, bool]]:
    """(trial, labeler) -> (judge passed, human passed), newest grade and label winning."""
    grader, version, _ = _identity(grades)
    newest_grade: dict[str, Grade] = {}
    for grade in grades:
        current = newest_grade.get(grade.trial_id)
        if grade.status == "ok" and (current is None or grade.id > current.id):
            newest_grade[grade.trial_id] = grade
    newest_label: dict[_ItemKey, HumanLabel] = {}
    for label in labels:
        if (label.grader, label.grader_version) != (grader, version):
            continue
        key = (label.trial_id, label.labeler)
        if key not in newest_label or label.created_at >= newest_label[key].created_at:
            newest_label[key] = label
    return {
        key: (newest_grade[key[0]].passed, label.passed)
        for key, label in newest_label.items()
        if key[0] in newest_grade
    }


def _arrays(
    items: dict[_ItemKey, tuple[bool, bool]], keys: Sequence[_ItemKey]
) -> tuple[BoolArray, BoolArray]:
    judge = np.array([items[key][0] for key in keys], dtype=bool)
    human = np.array([items[key][1] for key in keys], dtype=bool)
    return judge, human
