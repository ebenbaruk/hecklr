"""Scenario to graders, graders to a trial's grades, and grades to the trial's verdict."""

import re
from collections.abc import Sequence
from dataclasses import dataclass, replace
from typing import Final, Literal

import anyio

from hecklr_grade.base import errored
from hecklr_grade.custom import load_custom_grader
from hecklr_grade.email import EmailThreadingGrader
from hecklr_grade.judge import JudgeGrader, load_rubric
from hecklr_grade.safety import (
    InjectionResistedGrader,
    NoCrossCustomerDataGrader,
    NoUnauthorizedRefundGrader,
)
from hecklr_grade.state import StateGrader, check_expression
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
from hecklr_grade.voice import VoiceMetricGrader
from hecklr_schemas import Channel, Grade, GradeContext, Grader
from hecklr_schemas.scenario import (
    ArgsMatchCriterion,
    CallOrderCriterion,
    ContainsCriterion,
    CustomCriterion,
    EmailThreadingCriterion,
    InjectionResistedCriterion,
    JudgeCriterion,
    LatencyP95BelowCriterion,
    MaxTurnsCriterion,
    NoCrossCustomerDataCriterion,
    NotContainsCriterion,
    NoUnauthorizedRefundCriterion,
    RegexCriterion,
    Scenario,
    StateCriterion,
    SuccessCriterion,
    ToolCalledCriterion,
    ToolNotCalledCriterion,
    VoiceMetric,
    VoiceMetricCriterion,
)

Phase = Literal["deterministic", "judge", "voice"]
PHASES: Final[tuple[Phase, ...]] = ("deterministic", "judge", "voice")
"""Grading order (spec, run lifecycle step 4): free exact checks, then judges, then voice."""


@dataclass(frozen=True, slots=True)
class BoundGrader:
    """A grader bound to the criterion it answers (``None`` for a diagnostic) and its phase.

    It is itself a ``Grader``: ``grade`` hands the wrapped grader a context that carries the
    criterion and its index, which is how the frozen ``GradeContext`` tells a grader what to check.
    """

    grader: Grader
    phase: Phase
    criterion: SuccessCriterion | None = None
    criterion_index: int | None = None

    @property
    def name(self) -> str:
        """The wrapped grader's name."""
        return self.grader.name

    @property
    def version(self) -> str:
        """The wrapped grader's version."""
        return self.grader.version

    async def grade(self, ctx: GradeContext) -> Grade:
        """Grade with this binding's criterion, whatever criterion ``ctx`` carried."""
        return await self.grader.grade(self.bind(ctx))

    def bind(self, ctx: GradeContext) -> GradeContext:
        """``ctx`` with this binding's criterion and index."""
        return replace(ctx, criterion=self.criterion, criterion_index=self.criterion_index)


class InvalidCriterionError(ValueError):
    """A success criterion can never be graded (e.g. an unparsable regex or state expression)."""


def build_graders(scenario: Scenario, *, diagnostics: bool = True) -> list[BoundGrader]:
    """One grader per success criterion (in order), plus diagnostics for the scenario's channels.

    Diagnostics are the voice metrics on voice scenarios (gated on the spec's typical gates) and
    email threading on email scenarios, unless a criterion already covers them. They never decide
    a trial (``criterion_index`` is ``None``), but feed the reports (US-06).

    Raises:
        InvalidCriterionError: a regex, pattern or state expression cannot be parsed.
        UnknownRubricError: a judge names a rubric that does not exist.
        CustomGraderError: a custom grader is not installed or is not a ``Grader``.
    """
    bound = [
        BoundGrader(grader, phase, criterion, index)
        for index, criterion in enumerate(scenario.success)
        for grader, phase in [_grader_for(criterion)]
    ]
    if diagnostics:
        bound.extend(_diagnostics(scenario))
    return bound


def _grader_for(criterion: SuccessCriterion) -> tuple[Grader, Phase]:
    _validate(criterion)
    match criterion:
        case ContainsCriterion():
            return ContainsGrader(), "deterministic"
        case NotContainsCriterion():
            return NotContainsGrader(), "deterministic"
        case RegexCriterion():
            return RegexGrader(), "deterministic"
        case MaxTurnsCriterion():
            return MaxTurnsGrader(), "deterministic"
        case LatencyP95BelowCriterion():
            return LatencyP95BelowGrader(), "deterministic"
        case ToolCalledCriterion():
            return ToolCalledGrader(), "deterministic"
        case ToolNotCalledCriterion():
            return ToolNotCalledGrader(), "deterministic"
        case ArgsMatchCriterion():
            return ArgsMatchGrader(), "deterministic"
        case CallOrderCriterion():
            return CallOrderGrader(), "deterministic"
        case StateCriterion():
            return StateGrader(), "deterministic"
        case EmailThreadingCriterion():
            return EmailThreadingGrader(), "deterministic"
        case NoUnauthorizedRefundCriterion():
            return NoUnauthorizedRefundGrader(), "deterministic"
        case NoCrossCustomerDataCriterion():
            return NoCrossCustomerDataGrader(), "deterministic"
        case JudgeCriterion(rubric=rubric):
            return JudgeGrader(load_rubric(rubric)), "judge"
        case InjectionResistedCriterion():
            return InjectionResistedGrader(), "judge"
        case CustomCriterion(name=name):
            # Custom graders may call models, so they run with the judges.
            return load_custom_grader(name), "judge"
        case VoiceMetricCriterion(metric=metric):
            return VoiceMetricGrader(metric), "voice"


def _validate(criterion: SuccessCriterion) -> None:
    patterns: list[str] = []
    if isinstance(criterion, RegexCriterion):
        patterns.append(criterion.pattern)
    elif isinstance(criterion, ArgsMatchCriterion):
        patterns.extend(criterion.patterns.values())
    elif isinstance(criterion, StateCriterion):
        try:
            check_expression(criterion.expr)
        except ValueError as exc:
            raise InvalidCriterionError(str(exc)) from exc
    for pattern in patterns:
        try:
            re.compile(pattern)
        except re.error as exc:
            raise InvalidCriterionError(f"invalid regex {pattern!r}: {exc}") from exc


def _diagnostics(scenario: Scenario) -> list[BoundGrader]:
    criteria = scenario.success
    diagnostics: list[BoundGrader] = []
    if Channel.VOICE in scenario.channels:
        covered = {c.metric for c in criteria if isinstance(c, VoiceMetricCriterion)}
        diagnostics.extend(
            BoundGrader(VoiceMetricGrader(metric), "voice")
            for metric in VoiceMetric
            if metric not in covered
        )
    if Channel.EMAIL in scenario.channels and not any(
        isinstance(c, EmailThreadingCriterion) for c in criteria
    ):
        diagnostics.append(BoundGrader(EmailThreadingGrader(), "deterministic"))
    return diagnostics


async def grade_trial(ctx: GradeContext, graders: Sequence[BoundGrader]) -> list[Grade]:
    """Run every grader on one trial and return their grades in the order of ``graders``.

    Phases run in ``PHASES`` order; the graders of one phase run concurrently (graders only read
    the trial). A grader that raises yields an ``error`` grade instead of failing the trial's
    grading, so one broken custom grader cannot hide every other grade.
    """
    grades: dict[int, Grade] = {}

    async def run(index: int, bound: BoundGrader) -> None:
        bound_ctx = bound.bind(ctx)
        try:
            grades[index] = await bound.grader.grade(bound_ctx)
        except Exception as exc:  # noqa: BLE001 - surfaced as an error grade, never swallowed
            grades[index] = errored(bound_ctx, bound, f"grader raised {type(exc).__name__}: {exc}")

    for phase in PHASES:
        async with anyio.create_task_group() as group:
            for index, bound in enumerate(graders):
                if bound.phase == phase:
                    group.start_soon(run, index, bound)
    return [grades[index] for index in range(len(graders))]


def trial_passed(grades: Sequence[Grade]) -> bool:
    """Whether the trial passed: every ``ok`` success-criterion grade passed.

    ``skipped`` and ``error`` grades and diagnostics do not decide a trial (``Grade`` docstring);
    a trial with no ``ok`` criterion grade at all has nothing that shows it passed, so it fails.
    """
    deciding = [g for g in grades if g.criterion_index is not None and g.status == "ok"]
    return bool(deciding) and all(grade.passed for grade in deciding)
