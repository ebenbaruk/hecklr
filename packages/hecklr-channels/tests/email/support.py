"""Shared pieces of the email driver tests: briefs, tiny email agents, and threading grading."""

from collections.abc import Sequence
from datetime import UTC, datetime
from decimal import Decimal

from hecklr_adapters import AgentSession
from hecklr_adapters.email import make_reply
from hecklr_grade import EmailThreadingGrader
from hecklr_schemas import (
    Channel,
    EmailAttachment,
    EmailMessage,
    EmailPayload,
    Event,
    FakeClock,
    Grade,
    GradeContext,
    Persona,
    RunId,
    Scenario,
    SessionContext,
    Trial,
    TrialBrief,
    WorldSnapshot,
    new_id,
)

from ..fakes import TRIAL_ID

AGENT = "support@acme.test"
SEVEN_MINUTES_MS = 7 * 60 * 1000


def email_brief(
    *, max_turns: int = 24, max_duration_s: int = 3600, persona_id: str = "busy"
) -> TrialBrief:
    scenario = Scenario.model_validate(
        {
            "id": "reschedule",
            "title": "Move a visit",
            "channels": ["email"],
            "persona": persona_id,
            "world": "acme",
            "goal": "Move Thursday's visit",
            "success": [{"type": "email_threading"}],
            "limits": {"max_turns": max_turns, "max_duration_s": max_duration_s},
        }
    )
    persona = Persona.model_validate({"id": persona_id, "description": "short on time"})
    return TrialBrief(
        trial_id=TRIAL_ID, scenario=scenario, persona=persona, channel=Channel.EMAIL, seed=3
    )


def email_context(timeout_s: float = 5.0) -> SessionContext:
    return SessionContext(trial_id=TRIAL_ID, seed=3, timeout_s=timeout_s)


def answer_for(msg: EmailMessage) -> str:
    """What every test agent says: it echoes the customer's newest line."""
    return f"Thanks, noted: {msg.body.splitlines()[0]}"


def threaded_reply(msg: EmailMessage, *, from_addr: str = AGENT) -> EmailMessage:
    """The clean agent's reply."""
    return make_reply(msg, body=answer_for(msg), from_addr=from_addr)


def broken_reply(msg: EmailMessage) -> EmailMessage:
    """Fault F11: the reply loses the thread (no In-Reply-To, no References)."""
    return threaded_reply(msg).model_copy(update={"in_reply_to": None, "references": []})


class ThreadedAgent:
    """An in-process email agent that answers every email after ``delay_ms`` of virtual time."""

    def __init__(
        self,
        clock: FakeClock | None = None,
        *,
        delay_ms: int = SEVEN_MINUTES_MS,
        broken: bool = False,
        attachments: Sequence[EmailAttachment] = (),
    ) -> None:
        self.clock = clock
        self.delay_ms = delay_ms
        self.broken = broken
        self.attachments = list(attachments)
        self.received: list[EmailMessage] = []
        self.sessions: list[str] = []

    async def __call__(self, session: AgentSession, msg: EmailMessage) -> EmailMessage:
        self.received.append(msg)
        self.sessions.append(session.id)
        if self.clock is not None:
            self.clock.advance(self.delay_ms)
        reply = broken_reply(msg) if self.broken else threaded_reply(msg)
        return reply.model_copy(update={"attachments": self.attachments})


def emails(events: Sequence[Event], actor: str | None = None) -> list[tuple[Event, EmailMessage]]:
    return [
        (event, event.payload.message)
        for event in events
        if isinstance(event.payload, EmailPayload) and (actor is None or event.actor == actor)
    ]


async def grade_threading(events: Sequence[Event], brief: TrialBrief) -> Grade:
    """Run the ``email_threading`` grader on a finished email trial."""
    empty = WorldSnapshot(world_id="acme", taken_at_ms=0, collections={})
    trial = Trial(
        id=brief.trial_id,
        run_id=RunId(new_id()),
        scenario_id=brief.scenario.id,
        persona_id=brief.persona.id,
        channel=Channel.EMAIL,
        index=0,
        seed=brief.seed,
        status="ok",
        started_at=datetime(2026, 10, 21, tzinfo=UTC),
        duration_ms=0,
        cost_usd=Decimal(0),
        world_before=empty,
        world_after=empty,
    )
    ctx = GradeContext(
        trial=trial,
        events=list(events),
        scenario=brief.scenario,
        persona=brief.persona,
        criterion=brief.scenario.success[0],
        criterion_index=0,
    )
    return await EmailThreadingGrader().grade(ctx)
