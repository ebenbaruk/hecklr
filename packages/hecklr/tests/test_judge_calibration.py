"""``hecklr.judge_calibration``: labeled transcripts turned into trials the judges grade (SC7)."""

import functools
from datetime import UTC, datetime
from pathlib import Path

import anyio
import pytest
from pydantic import ValidationError

from hecklr.judge_calibration import (
    DRAFT_LABELER,
    CalibrationCase,
    CalibrationSet,
    CalibrationSetError,
    append_label,
    by_labeler,
    case_trial_id,
    describe,
    grade_context,
    judge_set,
    labeled_pairs,
    load_set,
    make_label,
    now,
    read_labels,
    rubric,
)
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    EmailPayload,
    HandoffPayload,
    ToolResultPayload,
    UtterancePayload,
)

WORLD = """\
id: tiny
collections:
  customers:
    key: customer_id
    records:
      - { customer_id: C-1, name: Ada, postcode: "75001" }
  appointments:
    key: appointment_id
    records:
      - { appointment_id: A-1, customer_id: C-1, slot: "2026-10-27T09:00", status: booked }
tools:
  - name: verify_customer
    description: Verify a caller.
    params:
      type: object
      required: [customer_id, postcode]
      properties: { customer_id: { type: string }, postcode: { type: string } }
    op: { kind: find, collection: customers }
  - name: cancel_appointment
    description: Cancel a visit.
    params:
      type: object
      required: [appointment_id, status]
      properties: { appointment_id: { type: string }, status: { const: cancelled } }
    op: { kind: update, collection: appointments }
"""

CHAT_CASE = """\
- id: chat-cancel
  channel: chat
  goal: Cancel your visit.
  facts: {customer_id: C-1, postcode: "75001"}
  rubrics:
    resolved: {}
    tone: {borderline: true}
  turns:
    - customer: Cancel my visit please. C-1, 75001.
    - tool: verify_customer
      args: {customer_id: C-1, postcode: "75001"}
    - tool: cancel_appointment
      args: {appointment_id: A-1, status: cancelled}
    - agent: Your visit on 27 October is cancelled.
"""


def _write_set(root: Path, cases: str = CHAT_CASE, labels: str = "") -> Path:
    (root / "cases").mkdir(parents=True)
    (root / "world.yaml").write_text(WORLD, encoding="utf-8")
    (root / "cases" / "a.yaml").write_text(cases, encoding="utf-8")
    (root / "labels.jsonl").write_text(labels, encoding="utf-8")
    (root / "set.yaml").write_text(
        "version: 1\nworld: world.yaml\ncases: cases/*.yaml\nlabels: labels.jsonl\n",
        encoding="utf-8",
    )
    return root


@pytest.fixture
def tiny_set(tmp_path: Path) -> CalibrationSet:
    return load_set(_write_set(tmp_path / "set"))


def test_sc7_a_case_becomes_a_trial_with_real_tool_results_and_world_change(
    tiny_set: CalibrationSet,
) -> None:
    (case,) = tiny_set.cases
    ctx = grade_context(case, tiny_set.world, None, "fake/judge")

    results = [e.payload for e in ctx.events if isinstance(e.payload, ToolResultPayload)]
    assert results[0].result == {"customer_id": "C-1", "name": "Ada", "postcode": "75001"}
    assert ctx.trial.world_before.collections["appointments"]["A-1"]["status"] == "booked"
    assert ctx.trial.world_after.collections["appointments"]["A-1"]["status"] == "cancelled"
    assert ctx.trial.id == case_trial_id("chat-cancel")
    assert ctx.scenario.goal == "Cancel your visit."
    assert ctx.events[0].t_ms == 0
    assert [e.seq for e in ctx.events] == list(range(len(ctx.events)))


def test_sc7_case_ids_and_event_ids_are_stable_across_loads(tiny_set: CalibrationSet) -> None:
    (case,) = tiny_set.cases
    first = grade_context(case, tiny_set.world, None, None)
    second = grade_context(case, tiny_set.world, None, None)

    assert [e.id for e in first.events] == [e.id for e in second.events]
    assert first.trial.seed == second.trial.seed


def test_sc7_email_turns_are_threaded_emails(tmp_path: Path) -> None:
    case = """\
- id: mail
  channel: email
  subject: Visit
  goal: Ask about your visit.
  facts: {email: ada@example.com}
  rubrics: {tone: {}}
  turns:
    - customer: When is my visit?
    - agent: On 27 October.
"""
    calibration_set = load_set(_write_set(tmp_path / "set", case))
    events = grade_context(calibration_set.cases[0], calibration_set.world, None, None).events

    first, reply = (e.payload for e in events)
    assert isinstance(first, EmailPayload)
    assert isinstance(reply, EmailPayload)
    assert first.message.subject == "Visit"
    assert first.message.from_addr == "ada@example.com"
    assert reply.message.subject == "Re: Visit"
    assert reply.message.in_reply_to == first.message.message_id
    assert reply.message.to_addrs == ["ada@example.com"]


def test_sc7_voice_turns_can_be_cut_off_and_handoffs_are_events(tmp_path: Path) -> None:
    case = """\
- id: call
  channel: voice
  goal: Report a gas smell.
  rubrics: {handoff_appropriate: {}}
  turns:
    - customer: I smell gas!
    - agent: Please leave the house now and
      interrupted: true
    - handoff: Gas smell
      target: on-call engineer
"""
    calibration_set = load_set(_write_set(tmp_path / "set", case))
    events = grade_context(calibration_set.cases[0], calibration_set.world, None, None).events

    utterance = events[1].payload
    assert isinstance(utterance, UtterancePayload)
    assert utterance.interrupted
    assert events[2].payload == HandoffPayload(reason="Gas smell", target="on-call engineer")


def test_sc7_faults_and_expected_refusals_become_tool_errors(tmp_path: Path) -> None:
    case = """\
- id: faults
  channel: chat
  goal: Cancel your visit.
  rubrics: {resolved: {}}
  turns:
    - customer: Cancel please, C-1, 75002.
    - tool: verify_customer
      args: {customer_id: C-1, postcode: "75002"}
      expect_error: true
    - tool: cancel_appointment
      args: {appointment_id: A-1, status: cancelled}
      fault: timeout
    - tool: cancel_appointment
      args: {appointment_id: A-1, status: cancelled}
      fault: http_500
    - agent: Sorry, that did not work.
"""
    calibration_set = load_set(_write_set(tmp_path / "set", case))
    ctx = grade_context(calibration_set.cases[0], calibration_set.world, None, None)

    errors = [e.payload.error for e in ctx.events if isinstance(e.payload, ToolResultPayload)]
    assert errors[0] is not None
    assert errors[0].startswith("not_found: ")
    assert errors[1:] == [
        "timeout: the call was abandoned before it completed",
        "internal_error: Internal Server Error",
    ]
    assert ctx.trial.world_after.collections == ctx.trial.world_before.collections


@pytest.mark.parametrize(
    ("turn", "message"),
    [
        (
            '{tool: verify_customer, args: {customer_id: C-9, postcode: "1"}}',
            r"verify_customer was refused \(not_found",
        ),
        (
            '{tool: verify_customer, args: {customer_id: C-1, postcode: "75001"}, '
            "expect_error: true}",
            "succeeded but is marked expect_error",
        ),
    ],
)
def test_sc7_a_tool_call_that_does_not_behave_as_written_is_a_set_error(
    tmp_path: Path, turn: str, message: str
) -> None:
    case = f"""\
- id: typo
  channel: chat
  goal: Anything.
  rubrics: {{tone: {{}}}}
  turns:
    - customer: Hi.
    - {turn}
"""
    calibration_set = load_set(_write_set(tmp_path / "set", case))

    with pytest.raises(CalibrationSetError, match=message):
        grade_context(calibration_set.cases[0], calibration_set.world, None, None)


@pytest.mark.parametrize(
    ("case", "message"),
    [
        ({"rubrics": {"vibes": {}}}, "unknown rubrics"),
        ({"channel": "email"}, "e-mail cases need a subject"),
        ({"subject": "Hi"}, "e-mail cases need a subject"),
        ({"turns": [{"customer": "a", "interrupted": True}, {"agent": "b"}]}, "only voice"),
        ({"turns": [{"customer": "a", "agent": "b"}, {"agent": "c"}]}, "exactly one of"),
        ({"turns": [{"customer": "a", "args": {"x": 1}}, {"agent": "c"}]}, "belong to tool"),
    ],
)
def test_sc7_invalid_cases_are_refused(case: dict[str, object], message: str) -> None:
    base: dict[str, object] = {
        "id": "x",
        "channel": "chat",
        "goal": "g",
        "rubrics": {"tone": {}},
        "turns": [{"customer": "a"}, {"agent": "b"}],
    }
    with pytest.raises(ValidationError, match=message):
        CalibrationCase.model_validate({**base, **case})


@pytest.mark.parametrize(
    ("change", "message"),
    [
        (lambda root: (root / "set.yaml").write_text("version: 2\n"), "not a calibration set"),
        (lambda root: (root / "world.yaml").unlink(), "world"),
        (lambda root: (root / "cases" / "a.yaml").unlink(), "no case file matches"),
        (lambda root: (root / "cases" / "a.yaml").write_text("{x: 1}\n"), "holds a list"),
        (lambda root: (root / "cases" / "a.yaml").write_text("- [\n"), "invalid YAML"),
        (lambda root: (root / "cases" / "a.yaml").write_text("- {id: x}\n"), "case 0"),
        (
            lambda root: (root / "cases" / "b.yaml").write_text(CHAT_CASE),
            "used twice",
        ),
    ],
)
def test_sc7_a_malformed_set_names_the_problem(
    tmp_path: Path, change: object, message: str
) -> None:
    root = _write_set(tmp_path / "set")
    assert callable(change)
    change(root)

    with pytest.raises(CalibrationSetError, match=message):
        load_set(root)


def test_sc7_labels_round_trip_append_only_and_filter_by_labeler(
    tiny_set: CalibrationSet,
) -> None:
    (case,) = tiny_set.cases
    when = datetime(2026, 10, 23, tzinfo=UTC)
    draft = make_label(case, "tone", passed=True, labeler=DRAFT_LABELER, note="ok", created_at=when)
    human = make_label(case, "tone", passed=False, labeler="human:ada", note=None, created_at=when)

    append_label(tiny_set.labels_path, draft)
    append_label(tiny_set.labels_path, human)

    labels = read_labels(tiny_set.labels_path)
    assert labels == [draft, human]
    assert draft.grader == "judge:tone"
    assert draft.grader_version == rubric("tone").version
    assert by_labeler(labels, ["human:"]) == [human]
    assert by_labeler(labels, []) == labels


def test_sc7_labels_are_stamped_with_an_aware_time() -> None:
    assert now().tzinfo is not None


def test_sc7_a_malformed_or_missing_labels_file_is_a_set_error(tmp_path: Path) -> None:
    path = tmp_path / "labels.jsonl"
    path.write_text('\n{"passed": true}\n', encoding="utf-8")

    with pytest.raises(CalibrationSetError, match=r"labels.jsonl:2: not a human label"):
        read_labels(path)
    with pytest.raises(CalibrationSetError, match="cannot read labels"):
        read_labels(tmp_path / "missing.jsonl")


def test_sc7_no_policy_override_uses_the_safety_pack_rubric_with_the_world() -> None:
    assert rubric("no_policy_override").include_world
    assert rubric("injection_resisted").name == "injection_resisted"


def test_sc7_judge_set_grades_every_labeled_pair(tiny_set: CalibrationSet) -> None:
    judge = FakeLLM(
        responder=lambda request: {"verdict": "fail", "rationale": "No.", "evidence": []}
    )

    grades = anyio.run(judge_set, tiny_set, judge, "fake/judge")

    assert sorted(grade.grader for grade in grades) == ["judge:resolved", "judge:tone"]
    assert all(grade.status == "ok" and not grade.passed for grade in grades)
    assert {grade.trial_id for grade in grades} == {case_trial_id("chat-cancel")}


def test_sc7_judge_set_judges_only_the_given_pairs(tiny_set: CalibrationSet) -> None:
    judge = FakeLLM(
        responder=lambda request: {"verdict": "pass", "rationale": "Yes.", "evidence": []}
    )

    grades = anyio.run(
        functools.partial(judge_set, pairs={("chat-cancel", "tone")}), tiny_set, judge, "fake/j"
    )

    assert [grade.grader for grade in grades] == ["judge:tone"]
    assert len(judge.calls) == 1


def test_sc7_labeled_pairs_are_those_with_a_label_of_the_current_rubric_version(
    tiny_set: CalibrationSet,
) -> None:
    (case,) = tiny_set.cases
    when = datetime(2026, 10, 23, tzinfo=UTC)
    tone = make_label(case, "tone", passed=True, labeler="human:ada", note=None, created_at=when)
    resolved = make_label(
        case, "resolved", passed=True, labeler="human:ada", note=None, created_at=when
    )
    stale = resolved.model_copy(update={"grader_version": "old"})

    assert labeled_pairs(tiny_set, [tone, resolved]) == {
        ("chat-cancel", "tone"),
        ("chat-cancel", "resolved"),
    }
    assert labeled_pairs(tiny_set, [tone, stale]) == {("chat-cancel", "tone")}
    assert labeled_pairs(tiny_set, []) == set()


def test_sc7_describe_shows_the_transcript_and_what_changed(tiny_set: CalibrationSet) -> None:
    text = describe(tiny_set.cases[0], tiny_set.world)

    assert "Customer goal: Cancel your visit." in text
    assert "agent called cancel_appointment" in text
    assert "appointments['A-1'].status: changed \"cancelled\"" in text


def test_sc7_describe_says_when_nothing_changed(tmp_path: Path) -> None:
    case = """\
- id: hello
  channel: chat
  goal: Say hello.
  rubrics: {tone: {}}
  turns:
    - customer: Hello.
    - agent: Hello, how can I help?
"""
    calibration_set = load_set(_write_set(tmp_path / "set", case))

    assert describe(calibration_set.cases[0], calibration_set.world).endswith("World changes: none")
