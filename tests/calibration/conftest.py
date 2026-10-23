"""Fixtures for the judge calibration set (SC7): the set itself and scripted judges over it.

A scripted judge answers each judge request from the set's labels, so the whole calibration
pipeline (case -> trial -> judge prompt -> grade -> kappa with an interval) runs offline with a
judge whose agreement with the labels is known in advance.
"""

import re
from collections.abc import Callable
from pathlib import Path

import pytest

from hecklr.judge_calibration import (
    DRAFT_LABELER,
    CalibrationSet,
    grade_context,
    load_set,
    read_labels,
)
from hecklr_llm import FakeLLM, FakeReply
from hecklr_schemas import HumanLabel, LLMRequest

SET_DIR = Path(__file__).resolve().parents[2] / "calibration" / "judges"
_RUBRIC = re.compile(r"^# Rubric: (\S+)$", re.MULTILINE)
_EVENT_ID = re.compile(r"^\[([0-9A-Z]{26})\]", re.MULTILINE)

Verdict = Callable[[str, str, bool], bool]
"""Decides a scripted judge's verdict from (case id, rubric, draft label)."""


@pytest.fixture(scope="session")
def calibration_set() -> CalibrationSet:
    return load_set(SET_DIR)


@pytest.fixture(scope="session")
def draft_labels(calibration_set: CalibrationSet) -> list[HumanLabel]:
    return [
        label
        for label in read_labels(calibration_set.labels_path)
        if label.labeler == DRAFT_LABELER
    ]


@pytest.fixture(scope="session")
def scripted_judge(
    calibration_set: CalibrationSet, draft_labels: list[HumanLabel]
) -> Callable[[Verdict], FakeLLM]:
    """A ``FakeLLM`` judge whose verdict on each (case, rubric) is ``verdict(case, rubric, label)``.

    The case is recognised by the id of its first transcript event, which is stable per case.
    """
    contexts = {
        case.id: grade_context(case, calibration_set.world, None, None)
        for case in calibration_set.cases
    }
    first_event: dict[str, str] = {ctx.events[0].id: case_id for case_id, ctx in contexts.items()}
    trial_case: dict[str, str] = {ctx.trial.id: case_id for case_id, ctx in contexts.items()}
    labeled = {
        (trial_case[label.trial_id], label.grader.removeprefix("judge:")): label.passed
        for label in draft_labels
    }

    def make(verdict: Verdict) -> FakeLLM:
        def respond(request: LLMRequest) -> FakeReply:
            prompt = request.messages[0].content
            rubric = _RUBRIC.search(prompt)
            event = _EVENT_ID.search(prompt)
            assert rubric, "not a judge prompt"
            assert event, "no transcript in the judge prompt"
            case_id = first_event[event.group(1)]
            passed = verdict(case_id, rubric.group(1), labeled[(case_id, rubric.group(1))])
            return {
                "verdict": "pass" if passed else "fail",
                "rationale": "Scripted verdict.",
                "evidence": [event.group(1)],
            }

        return FakeLLM(responder=respond)

    return make
