"""Hecklr graders: deterministic, tool, state, LLM judge, voice and safety.

Typical use, once a trial has finished::

    graders = build_graders(scenario)                      # once per scenario; fails fast
    grades = await grade_trial(GradeContext(trial=trial, events=events, scenario=scenario,
                                            persona=persona, llm=llm, judge_model=judge_model),
                               graders)
    passed = trial_passed(grades)
"""

from hecklr_grade.custom import CustomGraderError, load_custom_grader
from hecklr_grade.email import EmailThreadingGrader
from hecklr_grade.factory import (
    PHASES,
    BoundGrader,
    InvalidCriterionError,
    build_graders,
    grade_trial,
    trial_passed,
)
from hecklr_grade.judge import (
    BUILTIN_RUBRICS,
    JudgeGrader,
    JudgeVerdict,
    Rubric,
    UnknownRubricError,
    load_rubric,
)
from hecklr_grade.safety import (
    InjectionResistedGrader,
    NoCrossCustomerDataGrader,
    NoUnauthorizedRefundGrader,
)
from hecklr_grade.state import (
    IsoDateTime,
    StateExpressionError,
    StateExpressionTimeoutError,
    StateGrader,
    StateLookupError,
    evaluate,
)
from hecklr_grade.text import (
    ContainsGrader,
    LatencyP95BelowGrader,
    MaxTurnsGrader,
    NotContainsGrader,
    RegexGrader,
)
from hecklr_grade.tools import (
    ArgsMatchGrader,
    CallOrderGrader,
    ToolCalledGrader,
    ToolNotCalledGrader,
)
from hecklr_grade.voice import TYPICAL_GATES, Measurement, VoiceMetricGrader, measure

__version__ = "0.1.0"

__all__ = [
    "BUILTIN_RUBRICS",
    "PHASES",
    "TYPICAL_GATES",
    "ArgsMatchGrader",
    "BoundGrader",
    "CallOrderGrader",
    "ContainsGrader",
    "CustomGraderError",
    "EmailThreadingGrader",
    "InjectionResistedGrader",
    "InvalidCriterionError",
    "IsoDateTime",
    "JudgeGrader",
    "JudgeVerdict",
    "LatencyP95BelowGrader",
    "MaxTurnsGrader",
    "Measurement",
    "NoCrossCustomerDataGrader",
    "NoUnauthorizedRefundGrader",
    "NotContainsGrader",
    "RegexGrader",
    "Rubric",
    "StateExpressionError",
    "StateExpressionTimeoutError",
    "StateGrader",
    "StateLookupError",
    "ToolCalledGrader",
    "ToolNotCalledGrader",
    "UnknownRubricError",
    "VoiceMetricGrader",
    "build_graders",
    "evaluate",
    "grade_trial",
    "load_custom_grader",
    "load_rubric",
    "measure",
    "trial_passed",
]
