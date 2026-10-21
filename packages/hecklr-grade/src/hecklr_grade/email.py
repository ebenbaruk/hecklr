"""Email threading: every agent reply stays in the customer's thread (RFC 5322 section 3.6.4)."""

import re

from hecklr_grade.base import make_grade, of_kind, skipped
from hecklr_schemas import Channel, EmailMessage, EmailPayload, Event, Grade, GradeContext

_REPLY_PREFIX = re.compile(r"^\s*((re|fw|fwd|aw|sv)\s*(\[\d+\])?\s*:\s*)+", re.IGNORECASE)


def normalized_subject(subject: str) -> str:
    """The subject without reply/forward prefixes, case or spacing differences."""
    return " ".join(_REPLY_PREFIX.sub("", subject).split()).casefold()


def threading_problem(
    reply: EmailMessage, earlier: dict[str, EmailMessage], root: str
) -> str | None:
    """Why ``reply`` breaks the thread, or ``None`` if it threads correctly.

    ``earlier`` maps the Message-ID of every message sent before ``reply`` to the message; ``root``
    is the normalized subject of the conversation's first message.
    """
    if reply.in_reply_to is None:
        return "no In-Reply-To"
    parent = earlier.get(reply.in_reply_to)
    if parent is None:
        return f"In-Reply-To {reply.in_reply_to} is not an earlier message of the thread"
    if not reply.references or reply.references[-1] != parent.message_id:
        return f"References does not end with the parent {parent.message_id}"
    missing = [ref for ref in parent.references if ref not in reply.references]
    if missing:
        return f"References drops the thread's {', '.join(missing)}"
    if normalized_subject(reply.subject) != root:
        return f"subject {reply.subject!r} left the thread"
    return None


class EmailThreadingGrader:
    """``email_threading``: In-Reply-To names an earlier message, References carries the chain.

    Also requires the subject (ignoring ``Re:``-style prefixes) to stay the thread's subject.
    """

    name = "email_threading"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Check every agent email of an email trial."""
        if ctx.trial.channel is not Channel.EMAIL:
            return skipped(ctx, self, "threading is checked on email trials only")
        emails = of_kind(ctx.events, EmailPayload)
        if not any(event.actor == "agent" for event, _ in emails):
            return skipped(ctx, self, "the agent sent no email")
        root = normalized_subject(emails[0][1].message.subject)
        earlier: dict[str, EmailMessage] = {}
        broken: list[Event] = []
        problems: list[str] = []
        replies = 0
        for event, payload in emails:
            if event.actor == "agent":
                replies += 1
                problem = threading_problem(payload.message, earlier, root)
                if problem is not None:
                    broken.append(event)
                    problems.append(f"{payload.message.message_id}: {problem}")
            earlier[payload.message.message_id] = payload.message
        return make_grade(
            ctx,
            self,
            passed=not broken,
            rationale=(
                f"{replies - len(broken)} of {replies} agent email(s) threaded"
                + (f"; {'; '.join(problems)}" if problems else "")
            ),
            evidence=broken,
        )
