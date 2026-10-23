"""The judge calibration set (SC7): labeled transcripts the built-in judges are scored against.

A set is a directory with a ``set.yaml`` manifest, case files (YAML lists of
``CalibrationCase``) and a labels file (JSONL of ``HumanLabel``). ``hecklr calibrate --judges
--set DIR`` judges every case with every rubric it is labeled for, then compares the verdicts
with the labels (ADR 00hja).

A case is written as a conversation: customer and agent turns, tool calls and handoffs. Tool calls
are *executed* against the set's world (the same declarative tools and store the sandbox serves),
so tool results and the "after" world state are exactly what a real trial would have recorded,
and a typo in a case fails loudly instead of becoming a silent tool error. Ids derive from the case
id, not the clock, so the judge prompts (and the response cache) are the same on every run.
"""

import hashlib
import json
from collections.abc import Collection, Iterator, Mapping, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Annotated, Final, Literal, Self

import anyio
import yaml
from pydantic import BaseModel, ConfigDict, Field, JsonValue, ValidationError, model_validator
from ulid import ULID

from hecklr_grade.judge import (
    BUILTIN_RUBRICS,
    INTERNAL_RUBRICS,
    JudgeGrader,
    Rubric,
    load_rubric,
    render_transcript,
)
from hecklr_grade.safety_pack import load_pack_rubric
from hecklr_schemas import (
    Actor,
    Channel,
    EmailMessage,
    EmailPayload,
    Event,
    EventId,
    Grade,
    GradeContext,
    GradeId,
    HandoffPayload,
    HumanLabel,
    JudgeCriterion,
    LabelId,
    LLMProvider,
    Persona,
    RunId,
    Scenario,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialId,
    UtterancePayload,
    WorldSnapshot,
)
from hecklr_schemas.common import Identifier
from hecklr_schemas.events import EventPayload
from hecklr_schemas.world import World
from hecklr_world import Store, ToolError, WorldConfigError, diff_snapshots, load_world
from hecklr_world.tools import Toolbox

CALIBRATED_RUBRICS: Final = (*BUILTIN_RUBRICS, *INTERNAL_RUBRICS, "no_policy_override")
"""Every rubric Hecklr ships: SC7 asks kappa >= 0.70 on each."""
MIN_LABELS_PER_RUBRIC: Final = 40
"""Labeled conversations per rubric the set must have, and SC7 evidence must rest on."""

MANIFEST: Final = "set.yaml"
DRAFT_LABELER: Final = "claude-draft"
"""Labels an AI wrote as a starting point; they are not human labels and not SC7 evidence."""
HUMAN_PREFIX: Final = "human:"
"""Labelers named ``human:<name>`` are people (``calibration/judges/review.py`` writes them)."""
SUPPORT_ADDRESS: Final = "support@acme-maintenance.example"
PERSONA_ID: Final = "calibration-customer"

_STEP_MS: Final[Mapping[Channel, int]] = {
    Channel.CHAT: 4_000,
    Channel.VOICE: 3_000,
    Channel.EMAIL: 900_000,
}
"""Time between two turns: a chat message, a spoken turn, a reply e-mail 15 minutes later."""
_TOOL_MS: Final = 300
_FAULT_ERRORS: Final[Mapping[str, str]] = {
    # The texts the sandbox returns for these faults (hecklr_world.session).
    "timeout": "timeout: the call was abandoned before it completed",
    "http_500": "internal_error: Internal Server Error",
}


class CalibrationSetError(ValueError):
    """The calibration set is malformed: a manifest, case or label the user must fix."""


class _Model(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class CaseTurn(_Model):
    """One step of a case: exactly one of ``customer``, ``agent``, ``tool`` or ``handoff``."""

    customer: str | None = None
    agent: str | None = None
    interrupted: bool = False
    """Voice: the turn was cut off by the other party."""
    tool: str | None = None
    args: dict[str, JsonValue] = Field(default_factory=dict)
    fault: Literal["timeout", "http_500"] | None = None
    """The tool call failed this way instead of running."""
    expect_error: bool = False
    """The world refuses this call (e.g. a wrong postcode); without it, a refusal is a typo."""
    handoff: str | None = None
    """A handoff to a human, with this reason."""
    target: str | None = None

    @model_validator(mode="after")
    def _one_kind(self) -> Self:
        kinds = [
            k for k in ("customer", "agent", "tool", "handoff") if getattr(self, k) is not None
        ]
        if len(kinds) != 1:
            raise ValueError(
                f"a turn is exactly one of customer, agent, tool, handoff; got {kinds}"
            )
        if self.tool is None and (self.args or self.fault or self.expect_error):
            raise ValueError("args, fault and expect_error belong to tool turns")
        return self

    @property
    def said(self) -> tuple[Actor, str] | None:
        """``(actor, text)`` of a customer or agent turn; ``None`` for tools and handoffs."""
        if self.customer is not None:
            return "user", self.customer
        if self.agent is not None:
            return "agent", self.agent
        return None


class RubricUse(_Model):
    """How a case is used for one rubric."""

    borderline: bool = False
    """A hard case: reasonable graders could disagree, and the label rationale says why."""


class CalibrationCase(_Model):
    """One conversation of the set, and the rubrics it is labeled for."""

    id: Identifier
    channel: Channel
    goal: Annotated[str, Field(min_length=1)]
    facts: dict[str, JsonValue] = Field(default_factory=dict)
    subject: str | None = None
    """E-mail cases: the subject of the customer's first e-mail."""
    rubrics: Annotated[dict[str, RubricUse], Field(min_length=1)]
    turns: Annotated[list[CaseTurn], Field(min_length=2)]

    @model_validator(mode="after")
    def _consistent(self) -> Self:
        unknown = sorted(set(self.rubrics) - set(CALIBRATED_RUBRICS))
        if unknown:
            raise ValueError(f"unknown rubrics {unknown}; known: {list(CALIBRATED_RUBRICS)}")
        if (self.channel is Channel.EMAIL) != (self.subject is not None):
            raise ValueError("e-mail cases need a subject, and only they have one")
        if self.channel is not Channel.VOICE and any(turn.interrupted for turn in self.turns):
            raise ValueError("only voice turns can be interrupted")
        return self


class SetManifest(_Model):
    """``set.yaml``: where the world, the cases and the labels are, relative to the set."""

    version: Literal[1]
    world: str
    cases: str
    """A glob of case files."""
    labels: str


@dataclass(frozen=True, slots=True)
class CalibrationSet:
    """A loaded set: the world its cases run in, the cases, and where its labels are."""

    root: Path
    world: World
    cases: tuple[CalibrationCase, ...]
    labels_path: Path

    def items(self) -> Iterator[tuple[CalibrationCase, str]]:
        """Every (case, rubric) pair the set is labeled for."""
        for case in self.cases:
            for rubric in case.rubrics:
                yield case, rubric


def load_set(root: Path) -> CalibrationSet:
    """Load the set in ``root``.

    Raises:
        CalibrationSetError: the manifest, the world or a case is invalid, or case ids repeat.
    """
    manifest = _load_manifest(root / MANIFEST)
    try:
        world = load_world(root / manifest.world)
    except (OSError, WorldConfigError) as exc:
        raise CalibrationSetError(f"{root / MANIFEST}: world: {exc}") from exc
    paths = sorted(root.glob(manifest.cases))
    if not paths:
        raise CalibrationSetError(f"{root / MANIFEST}: no case file matches {manifest.cases!r}")
    cases = [case for path in paths for case in _load_cases(path)]
    seen: set[str] = set()
    for case in cases:
        if case.id in seen:
            raise CalibrationSetError(f"case id {case.id!r} is used twice")
        seen.add(case.id)
    return CalibrationSet(root, world, tuple(cases), root / manifest.labels)


def _load_manifest(path: Path) -> SetManifest:
    try:
        return SetManifest.model_validate(yaml.safe_load(path.read_text(encoding="utf-8")))
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise CalibrationSetError(f"{path}: not a calibration set manifest: {exc}") from exc


def _load_cases(path: Path) -> list[CalibrationCase]:
    try:
        data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise CalibrationSetError(f"{path}: invalid YAML: {exc}") from exc
    if not isinstance(data, list):
        raise CalibrationSetError(f"{path}: a case file holds a list of cases")
    cases = []
    for index, item in enumerate(data):
        try:
            cases.append(CalibrationCase.model_validate(item))
        except ValidationError as exc:
            raise CalibrationSetError(f"{path}: case {index}: {exc}") from exc
    return cases


# --- rubrics and labels ---------------------------------------------------------------------------


def rubric(name: str) -> Rubric:
    """The rubric ``name`` exactly as Hecklr's graders load it (``no_policy_override`` too)."""
    if name == "no_policy_override":
        return load_pack_rubric(name, include_world=True)
    return load_rubric(name)


def grader_name(rubric_name: str) -> str:
    """The grader name labels and grades of the set carry, e.g. ``judge:resolved``."""
    return f"judge:{rubric_name}"


def stable_ulid(*parts: str | int) -> str:
    """A ULID determined by ``parts`` (not by the clock)."""
    digest = hashlib.sha256("\0".join(str(part) for part in parts).encode()).digest()
    return str(ULID.from_bytes(digest[:16]))


def case_trial_id(case_id: str) -> TrialId:
    """The trial id a case is judged (and labeled) as."""
    return TrialId(stable_ulid("calibration", case_id, "trial"))


def make_label(
    case: CalibrationCase,
    rubric_name: str,
    *,
    passed: bool,
    labeler: str,
    note: str | None,
    created_at: datetime,
) -> HumanLabel:
    """A label of ``case`` for the current version of ``rubric_name``.

    No grade exists when a set is labeled, so ``grade_id`` is derived from the case and rubric;
    calibration joins labels and grades on trial, grader and rubric version, never on grade id.
    """
    trial_id = case_trial_id(case.id)
    return HumanLabel(
        id=LabelId(stable_ulid("label", case.id, rubric_name, labeler, created_at.isoformat())),
        trial_id=trial_id,
        grade_id=GradeId(stable_ulid("calibration", case.id, rubric_name, "grade")),
        grader=grader_name(rubric_name),
        grader_version=rubric(rubric_name).version,
        passed=passed,
        labeler=labeler,
        note=note,
        created_at=created_at,
    )


def read_labels(path: Path) -> list[HumanLabel]:
    """Every label in the JSONL file ``path`` (blank lines skipped).

    Raises:
        CalibrationSetError: the file is missing or a line is not a ``HumanLabel``.
    """
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise CalibrationSetError(f"cannot read labels: {exc}") from exc
    labels = []
    for number, line in enumerate(lines, start=1):
        if not line.strip():
            continue
        try:
            labels.append(HumanLabel.model_validate_json(line))
        except ValidationError as exc:
            raise CalibrationSetError(
                f"{path}:{number}: not a human label: {exc.errors()[0]['msg']}"
            ) from exc
    return labels


def append_label(path: Path, label: HumanLabel) -> None:
    """Append ``label`` to the labels file; labels are never rewritten (the newest one wins)."""
    with path.open("a", encoding="utf-8") as out:
        out.write(label.model_dump_json() + "\n")


def by_labeler(labels: Sequence[HumanLabel], prefixes: Sequence[str]) -> list[HumanLabel]:
    """Labels whose labeler starts with one of ``prefixes``; all of them when there are none."""
    if not prefixes:
        return list(labels)
    return [label for label in labels if label.labeler.startswith(tuple(prefixes))]


def labeled_pairs(
    calibration_set: CalibrationSet, labels: Sequence[HumanLabel]
) -> set[tuple[str, str]]:
    """The (case id, rubric) pairs of the set with a label of the rubric's current version.

    Only these pairs can be calibrated: a label of an older rubric version judged other words.
    """
    keys = {(label.trial_id, label.grader, label.grader_version) for label in labels}
    return {
        (case.id, name)
        for case, name in calibration_set.items()
        if (case_trial_id(case.id), grader_name(name), rubric(name).version) in keys
    }


def now() -> datetime:
    """The current time, timezone-aware (labels record when they were written)."""
    return datetime.now(UTC)


# --- cases as trials ------------------------------------------------------------------------------


def grade_context(
    case: CalibrationCase, world: World, llm: LLMProvider | None, judge_model: str | None
) -> GradeContext:
    """The trial a judge sees for ``case``: its events, scenario and both world snapshots.

    Raises:
        CalibrationSetError: a tool call the case does not mark ``expect_error`` was refused.
    """
    trial_id = case_trial_id(case.id)
    store = Store(world)
    before = WorldSnapshot(world_id=world.id, taken_at_ms=0, collections=store.state())
    events = _events(case, trial_id, store, Toolbox(world))
    end_ms = events[-1].t_ms
    after = WorldSnapshot(world_id=world.id, taken_at_ms=end_ms, collections=store.state())
    trial = Trial(
        id=trial_id,
        run_id=RunId(stable_ulid("calibration", case.id, "run")),
        scenario_id=case.id,
        persona_id=PERSONA_ID,
        channel=case.channel,
        index=0,
        seed=int.from_bytes(hashlib.sha256(case.id.encode()).digest()[:4], "big"),
        status="ok",
        started_at=datetime(2026, 10, 21, 9, 0, tzinfo=UTC),
        duration_ms=end_ms,
        cost_usd=Decimal(0),
        world_before=before,
        world_after=after,
        turns=sum(turn.customer is not None or turn.agent is not None for turn in case.turns),
    )
    scenario = Scenario(
        id=case.id,
        title=f"Calibration case {case.id}",
        channels=[case.channel],
        persona=PERSONA_ID,
        world=world.id,
        goal=case.goal,
        facts=case.facts,
        success=[JudgeCriterion(type="judge", rubric="resolved")],
    )
    return GradeContext(
        trial=trial,
        events=events,
        scenario=scenario,
        persona=Persona(id=PERSONA_ID, description="A customer of Acme Maintenance."),
        llm=llm,
        judge_model=judge_model,
    )


def _events(case: CalibrationCase, trial_id: TrialId, store: Store, tools: Toolbox) -> list[Event]:
    events: list[Event] = []
    t_ms = 0
    spoken = 0

    def add(actor: Actor, payload: EventPayload) -> None:
        seq = len(events)
        event_id = EventId(stable_ulid("calibration", case.id, "event", seq))
        events.append(
            Event(id=event_id, trial_id=trial_id, seq=seq, t_ms=t_ms, actor=actor, payload=payload)
        )

    for index, turn in enumerate(case.turns):
        said = turn.said
        if said is not None:
            t_ms += _STEP_MS[case.channel] if events else 0
            add(said[0], _message(case, said[0], said[1], spoken, turn.interrupted))
            spoken += 1
        elif turn.tool is not None:
            t_ms += _TOOL_MS
            call_id = f"call-{index}"
            add("agent", ToolCallPayload(call_id=call_id, tool=turn.tool, args=turn.args))
            result, error = _run_tool(case, index, turn.tool, turn, store, tools)
            t_ms += _TOOL_MS
            add(
                "tool",
                ToolResultPayload(
                    call_id=call_id, tool=turn.tool, result=result, error=error, latency_ms=_TOOL_MS
                ),
            )
        else:
            add("agent", HandoffPayload(reason=turn.handoff, target=turn.target))
    return events


def _message(
    case: CalibrationCase, actor: Actor, text: str, turn: int, interrupted: bool
) -> UtterancePayload | EmailPayload:
    if case.subject is None:  # chat and voice (only e-mail cases have a subject)
        return UtterancePayload(text=text, turn=turn, interrupted=interrupted)
    customer = str(case.facts.get("email", "customer@example.org"))
    sender, recipient = (
        (customer, SUPPORT_ADDRESS) if actor == "user" else (SUPPORT_ADDRESS, customer)
    )
    subject = case.subject if turn == 0 else f"Re: {case.subject}"
    return EmailPayload(
        message=EmailMessage(
            message_id=f"<{case.id}.{turn}@calibration.hecklr.test>",
            in_reply_to=None if turn == 0 else f"<{case.id}.{turn - 1}@calibration.hecklr.test>",
            subject=subject,
            body=text,
            from_addr=sender,
            to_addrs=[recipient],
        )
    )


def _run_tool(
    case: CalibrationCase, index: int, tool: str, turn: CaseTurn, store: Store, tools: Toolbox
) -> tuple[JsonValue, str | None]:
    if turn.fault is not None:
        return None, _FAULT_ERRORS[turn.fault]
    try:
        result = tools.execute(store, tool, turn.args)
    except ToolError as exc:
        if not turn.expect_error:
            raise CalibrationSetError(
                f"case {case.id!r} turn {index}: {tool} was refused ({exc.code}: "
                f"{exc.message}); fix the call or mark it expect_error"
            ) from exc
        return None, f"{exc.code}: {exc.message}"
    if turn.expect_error:
        raise CalibrationSetError(
            f"case {case.id!r} turn {index}: {tool} succeeded but is marked expect_error"
        )
    return result, None


# --- judging --------------------------------------------------------------------------------------


async def judge_set(
    calibration_set: CalibrationSet,
    llm: LLMProvider,
    judge_model: str,
    *,
    pairs: Collection[tuple[str, str]] | None = None,
    concurrency: int = 8,
) -> list[Grade]:
    """One grade per (case, rubric) pair of the set, judged ``concurrency`` at a time.

    ``pairs`` (case id, rubric) limits the judging to those pairs, e.g. the ones with a usable
    label (``labeled_pairs``), so no judge call is paid for a pair that cannot be calibrated.

    Judge failures come back as ``error`` grades (they never count as agreement), never as a
    crash.

    Raises:
        CalibrationSetError: a case cannot be turned into a trial.
    """
    todo = [
        (case, name)
        for case, name in calibration_set.items()
        if pairs is None or (case.id, name) in pairs
    ]
    graders = {name: JudgeGrader(rubric(name)) for name in {name for _, name in todo}}
    judged = {case.id for case, _ in todo}
    contexts = {
        case.id: grade_context(case, calibration_set.world, llm, judge_model)
        for case in calibration_set.cases
        if case.id in judged
    }
    grades: list[Grade] = []
    limiter = anyio.CapacityLimiter(concurrency)

    async def judge(case_id: str, name: str) -> None:
        async with limiter:
            grades.append(await graders[name].grade(contexts[case_id]))

    async with anyio.create_task_group() as group:
        for case, name in todo:
            group.start_soon(judge, case.id, name)
    return grades


def describe(case: CalibrationCase, world: World) -> str:
    """The case as a reviewer reads it: goal, facts, transcript and what changed in the world.

    The full world state (which the ground-truth rubrics also see) is the set's world file.
    """
    ctx = grade_context(case, world, None, None)
    changes = [
        f"  {change.path}: {change.kind} {json.dumps(change.after, sort_keys=True)}"
        for change in diff_snapshots(ctx.trial.world_before, ctx.trial.world_after)
    ]
    return (
        f"Channel: {case.channel.value}\nCustomer goal: {case.goal}\n"
        f"Customer facts: {json.dumps(case.facts, sort_keys=True)}\n\n"
        f"{render_transcript(ctx.events)}\n\n"
        "World changes:" + ("\n" + "\n".join(changes) if changes else " none")
    )
