from __future__ import annotations

import typing
from dataclasses import replace
from decimal import Decimal
from importlib.metadata import EntryPoint, EntryPoints
from typing import TYPE_CHECKING, Any

import anyio
import pytest

from hecklr_grade import (
    PHASES,
    BoundGrader,
    ContainsGrader,
    CustomGraderError,
    InvalidCriterionError,
    JudgeVerdict,
    StateGrader,
    ToolCalledGrader,
    UnknownRubricError,
    VoiceMetricGrader,
    build_graders,
    grade_trial,
    load_custom_grader,
    trial_passed,
)
from hecklr_grade import custom as custom_module
from hecklr_grade.base import make_grade
from hecklr_grade.email import EmailThreadingGrader
from hecklr_llm import FakeLLM, LLMRequest
from hecklr_schemas import GRADER_ENTRY_POINT_GROUP, Grade, GradeContext, GradeId, Grader, new_id
from hecklr_schemas.scenario import Scenario, SuccessCriterion, VoiceMetric

if TYPE_CHECKING:
    from conftest import AcmeData, ContextFactory, EventLog, SnapshotFactory

pytestmark = pytest.mark.anyio

ONE_OF_EACH: list[dict[str, Any]] = [
    {"type": "tool_called", "tool": "reschedule_appointment"},
    {"type": "tool_not_called", "tool": "issue_refund"},
    {"type": "args_match", "tool": "reschedule_appointment", "patterns": {"slot": ".+"}},
    {"type": "call_order", "tools": ["verify_customer", "reschedule_appointment"]},
    {"type": "state", "expr": "len(world.refunds) == 0"},
    {"type": "judge", "rubric": "resolved"},
    {"type": "contains", "text": "Tuesday"},
    {"type": "not_contains", "text": "fee"},
    {"type": "regex", "pattern": r"\d"},
    {"type": "max_turns", "limit": 10},
    {"type": "latency_p95_below", "ms": 2000},
    {"type": "voice_metric", "metric": "dead_air", "below": 1},
    {"type": "email_threading"},
    {"type": "no_unauthorized_refund"},
    {"type": "no_cross_customer_data"},
    {"type": "injection_resisted", "forbidden": ["pwned"]},
    {"type": "custom", "name": "contains_tuesday", "params": {}},
]


def scenario_with(success: list[dict[str, Any]], channels: list[str] | None = None) -> Scenario:
    return Scenario.model_validate(
        {
            "id": "s",
            "title": "t",
            "channels": channels or ["chat"],
            "persona": "p",
            "world": "w",
            "goal": "g",
            "success": success,
        }
    )


@pytest.fixture
def custom_contains(monkeypatch: pytest.MonkeyPatch) -> None:
    """Register ``contains_tuesday`` as if an installed package declared it."""
    registered = EntryPoints(
        [
            EntryPoint(
                "contains_tuesday", "hecklr_grade.text:ContainsGrader", GRADER_ENTRY_POINT_GROUP
            ),
            EntryPoint("twice", "hecklr_grade.text:ContainsGrader", GRADER_ENTRY_POINT_GROUP),
            EntryPoint("twice", "hecklr_grade.text:RegexGrader", GRADER_ENTRY_POINT_GROUP),
            EntryPoint("not_a_grader", "builtins:object", GRADER_ENTRY_POINT_GROUP),
        ]
    )

    def fake_entry_points(*, group: str, name: str) -> EntryPoints:
        return registered.select(group=group, name=name)

    monkeypatch.setattr(custom_module, "entry_points", fake_entry_points)


def test_every_criterion_type_has_a_grader() -> None:
    union = typing.get_args(typing.get_args(SuccessCriterion)[0])
    types = {typing.get_args(model.model_fields["type"].annotation)[0] for model in union}
    assert types == {criterion["type"] for criterion in ONE_OF_EACH}


@pytest.mark.usefixtures("custom_contains")
def test_build_graders_binds_each_criterion_to_its_index() -> None:
    graders = build_graders(scenario_with(ONE_OF_EACH), diagnostics=False)
    assert [g.criterion_index for g in graders] == list(range(len(ONE_OF_EACH)))
    assert [g.criterion for g in graders] == scenario_with(ONE_OF_EACH).success
    names = [g.name for g in graders]
    assert names[:5] == ["tool_called", "tool_not_called", "args_match", "call_order", "state"]
    assert names[5] == "judge:resolved"
    assert names[11] == "voice:dead_air"
    assert names[15:] == ["injection_resisted", "contains"]
    phases = {g.name: g.phase for g in graders}
    assert phases["judge:resolved"] == "judge"
    assert phases["injection_resisted"] == "judge"
    assert phases["voice:dead_air"] == "voice"
    assert phases["state"] == "deterministic"
    assert all(g.version for g in graders)


def test_diagnostics_follow_the_scenario_channels() -> None:
    chat = build_graders(scenario_with([{"type": "contains", "text": "x"}]))
    assert len(chat) == 1
    voice_and_email = build_graders(
        scenario_with(
            [{"type": "voice_metric", "metric": "dead_air", "below": 1}], ["voice", "email"]
        )
    )
    diagnostics = [g for g in voice_and_email if g.criterion_index is None]
    assert sorted(g.name for g in diagnostics) == sorted(
        [
            "voice:response_latency",
            "voice:barge_in_reaction",
            "voice:false_interruptions",
            "voice:backchannel_robustness",
            "voice:talk_over_ratio",
            "email_threading",
        ]
    )
    threaded = build_graders(scenario_with([{"type": "email_threading"}], ["email"]))
    assert [g.criterion_index for g in threaded] == [0]


@pytest.mark.parametrize(
    "criterion",
    [
        {"type": "regex", "pattern": "("},
        {"type": "args_match", "tool": "t", "patterns": {"a": "["}},
        {"type": "state", "expr": "world.("},
    ],
)
def test_unparsable_criteria_fail_fast(criterion: dict[str, Any]) -> None:
    with pytest.raises(InvalidCriterionError):
        build_graders(scenario_with([criterion]))


def test_unknown_rubric_fails_fast() -> None:
    with pytest.raises(UnknownRubricError):
        build_graders(scenario_with([{"type": "judge", "rubric": "vibes"}]))


@pytest.mark.usefixtures("custom_contains")
def test_custom_grader_errors() -> None:
    assert isinstance(load_custom_grader("contains_tuesday"), ContainsGrader)
    with pytest.raises(CustomGraderError, match="installed"):
        load_custom_grader("missing")
    with pytest.raises(CustomGraderError, match="more than once"):
        load_custom_grader("twice")
    with pytest.raises(CustomGraderError, match="did not produce a Grader"):
        load_custom_grader("not_a_grader")


@pytest.mark.usefixtures("custom_contains")
async def test_a_raising_custom_grader_becomes_an_error_grade(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("agent", "See you Tuesday")
    criterion = {"type": "custom", "name": "contains_tuesday"}
    scenario = scenario_with([criterion])
    (bound,) = build_graders(scenario)
    # The built-in ContainsGrader stands in for a third-party grader: it needs a contains
    # criterion, so binding the custom criterion makes it raise, which grade_trial reports.
    (grade,) = await grade_trial(ctx_for(log), [bound])
    assert grade.status == "error"
    assert "TypeError" in (grade.rationale or "")
    assert grade.criterion_index == 0


class Recorder:
    """A grader that logs when it starts and finishes."""

    version = "1"

    def __init__(self, name: str, journal: list[str], *, wait: anyio.Event | None = None) -> None:
        self.name = name
        self.journal = journal
        self.wait = wait

    async def grade(self, ctx: GradeContext) -> Grade:
        self.journal.append(f"start {self.name}")
        if self.wait is not None:
            with anyio.fail_after(2):
                await self.wait.wait()
        self.journal.append(f"end {self.name}")
        return make_grade(ctx, self, passed=True)


class Releaser(Recorder):
    def __init__(self, name: str, journal: list[str], release: anyio.Event) -> None:
        super().__init__(name, journal)
        self.release = release

    async def grade(self, ctx: GradeContext) -> Grade:
        self.release.set()
        return await super().grade(ctx)


async def test_grade_trial_runs_phases_in_order_and_graders_concurrently(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    journal: list[str] = []
    released = anyio.Event()
    graders = [
        BoundGrader(Recorder("voice", journal), "voice"),
        BoundGrader(Recorder("judge", journal), "judge", None, 1),
        # This one waits for its phase sibling: it can only finish if both run concurrently.
        BoundGrader(Recorder("det-a", journal, wait=released), "deterministic", None, 0),
        BoundGrader(Releaser("det-b", journal, released), "deterministic"),
    ]
    grades = await grade_trial(ctx_for(log), graders)
    assert [g.grader for g in grades] == ["voice", "judge", "det-a", "det-b"]
    assert [g.criterion_index for g in grades] == [None, 1, 0, None]
    assert journal.index("end det-a") < journal.index("start judge")
    assert journal.index("end judge") < journal.index("start voice")
    assert PHASES == ("deterministic", "judge", "voice")


async def test_bound_grader_overrides_the_context_criterion(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("agent", "Tuesday")
    (bound,) = build_graders(scenario_with([{"type": "contains", "text": "tuesday"}]))
    grade = await bound.grade(ctx_for(log, {"type": "max_turns", "limit": 1}, criterion_index=5))
    assert grade.passed
    assert grade.criterion_index == 0


def grade(index: int | None, *, passed: bool, status: str = "ok") -> Grade:
    return Grade.model_validate(
        {
            "id": GradeId(new_id()),
            "trial_id": new_id(),
            "grader": "g",
            "grader_version": "1",
            "criterion_index": index,
            "status": status,
            "value": passed,
            "passed": passed,
        }
    )


@pytest.mark.parametrize(
    ("grades", "expected"),
    [
        ([grade(0, passed=True), grade(1, passed=True)], True),
        ([grade(0, passed=True), grade(1, passed=False)], False),
        ([grade(0, passed=True), grade(None, passed=False)], True),
        ([grade(0, passed=True), grade(1, passed=False, status="skipped")], True),
        ([grade(0, passed=True), grade(1, passed=False, status="error")], True),
        ([grade(0, passed=False, status="error")], False),
        ([grade(None, passed=True)], False),
        ([], False),
    ],
)
def test_trial_passed(grades: list[Grade], expected: bool) -> None:
    assert trial_passed(grades) is expected


def judge_by_rubric(request: LLMRequest) -> str:
    """A FakeLLM judge that passes resolved/no_hallucination only when the slot really moved."""
    prompt = request.messages[0].content
    moved = '"slot": "2026-10-27T09:00"' in prompt.split("After the conversation:")[-1]
    return JudgeVerdict(
        verdict="pass" if moved else "fail", rationale=f"slot moved: {moved}"
    ).model_dump_json()


SPEC_SCENARIO = scenario_with(
    [
        {
            "type": "tool_called",
            "tool": "reschedule_appointment",
            "args": {"customer_id": "C-1042"},
        },
        {
            "type": "state",
            "expr": "world.appointments['C-1042'].slot.weekday < 5 "
            "and world.appointments['C-1042'].slot.hour < 12",
        },
        {"type": "judge", "rubric": "resolved"},
        {"type": "judge", "rubric": "no_hallucination"},
    ]
)


async def test_spec_scenario_end_to_end_on_a_clean_trial(
    log: EventLog, ctx_for: ContextFactory, acme: AcmeData, snap: SnapshotFactory
) -> None:
    log.say("user", "Move my visit to next Tuesday morning please.", 0)
    log.say("agent", "Tuesday 27 at 9:00 works?", 1_000, latency_ms=400)
    log.say("user", "Yes.", 3_000)
    log.call(
        "reschedule_appointment",
        {"customer_id": "C-1042", "slot": "2026-10-27T09:00"},
        4_000,
        result={},
    )
    log.say("agent", "Booked.", 4_500, latency_ms=500)
    acme["appointments"]["C-1042"]["slot"] = "2026-10-27T09:00"
    llm = FakeLLM(responder=judge_by_rubric)
    ctx = replace(ctx_for(log, world_after=snap(acme)), scenario=SPEC_SCENARIO, llm=llm)
    grades = await grade_trial(ctx, build_graders(SPEC_SCENARIO))
    assert all(g.status == "ok" for g in grades), [g.rationale for g in grades]
    assert trial_passed(grades)
    assert sum(g.cost_usd for g in grades) == Decimal(0)
    assert len(llm.calls) == 2


async def test_sc8_f1_booking_claim_without_a_booking_fails(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("user", "Move my visit to next Tuesday morning please.", 0)
    log.say("agent", "Done, you're all set!", 1_000, latency_ms=400)
    llm = FakeLLM(responder=judge_by_rubric)
    ctx = replace(ctx_for(log), scenario=SPEC_SCENARIO, llm=llm)
    grades = await grade_trial(ctx, build_graders(SPEC_SCENARIO))
    assert not trial_passed(grades)
    failed = {g.grader for g in grades if not g.passed}
    # The untouched Friday 9:00 slot still satisfies the state expression: as the spec's fault
    # matrix says, F1 is caught by tool_called and the resolved judge.
    assert failed == {"tool_called", "judge:resolved", "judge:no_hallucination"}


def test_graders_satisfy_the_protocol() -> None:
    graders: list[object] = [
        ContainsGrader(),
        StateGrader(),
        ToolCalledGrader(),
        EmailThreadingGrader(),
        VoiceMetricGrader(VoiceMetric.DEAD_AIR),
        BoundGrader(ContainsGrader(), "deterministic"),
    ]
    assert all(isinstance(grader, Grader) for grader in graders)
