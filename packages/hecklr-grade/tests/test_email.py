from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from hecklr_grade.email import EmailThreadingGrader, normalized_subject
from hecklr_schemas import Channel, EmailMessage

if TYPE_CHECKING:
    from conftest import ContextFactory, EventLog

pytestmark = pytest.mark.anyio

CUSTOMER = "camille@acme.test"
SUPPORT = "support@acme.test"


def customer_mail(
    message_id: str, subject: str, *, parent: EmailMessage | None = None
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        in_reply_to=parent.message_id if parent else None,
        references=[*parent.references, parent.message_id] if parent else [],
        subject=subject,
        body="Can I move my visit?",
        from_addr=CUSTOMER,
        to_addrs=[SUPPORT],
    )


def reply_to(
    parent: EmailMessage,
    message_id: str,
    *,
    in_reply_to: str | None = "parent",
    references: list[str] | None = None,
    subject: str | None = None,
) -> EmailMessage:
    return EmailMessage(
        message_id=message_id,
        in_reply_to=parent.message_id if in_reply_to == "parent" else in_reply_to,
        references=[*parent.references, parent.message_id] if references is None else references,
        subject=f"Re: {parent.subject}" if subject is None else subject,
        body="Done, see you Tuesday.",
        from_addr=SUPPORT,
        to_addrs=[CUSTOMER],
    )


async def test_a_correct_two_round_thread_passes(log: EventLog, ctx_for: ContextFactory) -> None:
    first = customer_mail("<c1@acme.test>", "Boiler visit")
    log.email("user", first, 0)
    answer = reply_to(first, "<a1@acme.test>")
    log.email("agent", answer, 1_000)
    second = customer_mail("<c2@acme.test>", "RE: Boiler visit", parent=answer)
    log.email("user", second, 2_000)
    log.email("agent", reply_to(second, "<a2@acme.test>", subject="Re: re: boiler  VISIT"), 3_000)
    grade = await EmailThreadingGrader().grade(
        ctx_for(log, {"type": "email_threading"}, channel=Channel.EMAIL)
    )
    assert grade.passed, grade.rationale
    assert "2 of 2" in (grade.rationale or "")


@pytest.mark.parametrize(
    ("overrides", "problem"),
    [
        ({"in_reply_to": None}, "no In-Reply-To"),  # F11
        ({"in_reply_to": "<unknown@x>"}, "not an earlier message"),
        ({"references": []}, "References does not end"),
        ({"references": ["<c1@acme.test>"]}, "drops the thread's <c0@acme.test>"),
        ({"subject": "Your appointment"}, "left the thread"),
    ],
)
async def test_sc8_f11_broken_threading_fails(
    log: EventLog, ctx_for: ContextFactory, overrides: dict[str, Any], problem: str
) -> None:
    root = customer_mail("<c0@acme.test>", "Boiler visit")
    log.email("user", root, 0)
    first = customer_mail("<c1@acme.test>", "Re: Boiler visit", parent=root)
    log.email("user", first, 500)
    bad = log.email("agent", reply_to(first, "<a1@acme.test>", **overrides), 1_000)
    grade = await EmailThreadingGrader().grade(
        ctx_for(log, {"type": "email_threading"}, channel=Channel.EMAIL)
    )
    assert not grade.passed
    assert problem in (grade.rationale or "")
    assert [ref.event_id for ref in grade.evidence] == [bad.id]


async def test_skipped_off_email_or_without_agent_mail(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.email("user", customer_mail("<c1@acme.test>", "Hello"), 0)
    grader = EmailThreadingGrader()
    assert (await grader.grade(ctx_for(log, channel=Channel.EMAIL))).status == "skipped"
    assert (await grader.grade(ctx_for(log))).status == "skipped"


def test_normalized_subject() -> None:
    assert normalized_subject("RE: Fwd:  re[2]: Boiler   Visit ") == "boiler visit"
    assert normalized_subject("Reply needed") == "reply needed"
