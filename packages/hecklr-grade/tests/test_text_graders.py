from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from hecklr_grade.text import (
    ContainsGrader,
    LatencyP95BelowGrader,
    MaxTurnsGrader,
    NotContainsGrader,
    RegexGrader,
)
from hecklr_schemas import Channel, EmailMessage

if TYPE_CHECKING:
    from conftest import ContextFactory, EventLog

pytestmark = pytest.mark.anyio


async def test_contains_passes_case_insensitively_and_cites_the_message(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("user", "Can I move my visit?")
    hit = log.say("agent", "Your visit is now on TUESDAY at 9:00.")
    grade = await ContainsGrader().grade(ctx_for(log, {"type": "contains", "text": "tuesday"}))
    assert grade.passed
    assert grade.status == "ok"
    assert [ref.event_id for ref in grade.evidence] == [hit.id]
    assert grade.criterion_index == 0
    assert grade.grader == "contains"


async def test_contains_fails_when_only_the_other_speaker_says_it(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("user", "Tuesday please")
    log.say("agent", "Sure")
    grade = await ContainsGrader().grade(ctx_for(log, {"type": "contains", "text": "Tuesday"}))
    assert not grade.passed
    assert grade.evidence == []


async def test_contains_respects_case_sensitivity(log: EventLog, ctx_for: ContextFactory) -> None:
    log.say("agent", "tuesday")
    criterion = {"type": "contains", "text": "Tuesday", "case_sensitive": True}
    assert not (await ContainsGrader().grade(ctx_for(log, criterion))).passed


async def test_contains_reads_email_subject_and_body(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.email(
        "agent",
        EmailMessage(
            message_id="<a@acme.test>",
            subject="Refund confirmed",
            body="Done.",
            from_addr="support@acme.test",
            to_addrs=["camille@acme.test"],
        ),
    )
    grade = await ContainsGrader().grade(ctx_for(log, {"type": "contains", "text": "refund"}))
    assert grade.passed


async def test_not_contains_fails_and_cites_every_offending_message(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    first = log.say("agent", "There is a cancellation fee of 30 euros.")
    log.say("user", "What?")
    second = log.say("agent", "Yes, the Cancellation Fee applies.")
    grade = await NotContainsGrader().grade(
        ctx_for(log, {"type": "not_contains", "text": "cancellation fee"})
    )
    assert not grade.passed
    assert [ref.event_id for ref in grade.evidence] == [first.id, second.id]


async def test_not_contains_passes_when_never_said(log: EventLog, ctx_for: ContextFactory) -> None:
    log.say("agent", "No fee.")
    grade = await NotContainsGrader().grade(ctx_for(log, {"type": "not_contains", "text": "euros"}))
    assert grade.passed


async def test_regex_searches_messages(log: EventLog, ctx_for: ContextFactory) -> None:
    log.say("agent", "Booked for 2026-10-27 at 09:00")
    passing = {"type": "regex", "pattern": r"\d{4}-\d{2}-\d{2}"}
    failing = {"type": "regex", "pattern": r"^\d{2}/\d{2}"}
    assert (await RegexGrader().grade(ctx_for(log, passing))).passed
    assert not (await RegexGrader().grade(ctx_for(log, failing))).passed


async def test_regex_ignore_case(log: EventLog, ctx_for: ContextFactory) -> None:
    log.say("agent", "BOOKED")
    criterion = {"type": "regex", "pattern": "booked", "ignore_case": True}
    assert (await RegexGrader().grade(ctx_for(log, criterion))).passed


async def test_invalid_regex_is_an_error_grade_not_a_crash(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("agent", "x")
    grade = await RegexGrader().grade(ctx_for(log, {"type": "regex", "pattern": "("}))
    assert grade.status == "error"
    assert not grade.passed


async def test_max_turns_counts_agent_turns_and_cites_the_excess(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    for index in range(3):
        log.say("user", f"q{index}", t_ms=index * 1000)
        log.say("agent", f"a{index}", t_ms=index * 1000 + 500)
    within = await MaxTurnsGrader().grade(ctx_for(log, {"type": "max_turns", "limit": 3}))
    over = await MaxTurnsGrader().grade(ctx_for(log, {"type": "max_turns", "limit": 2}))
    assert within.passed
    assert within.value == 3.0
    assert not over.passed
    assert [ref.event_id for ref in over.evidence] == [log.events[-1].id]


async def test_latency_p95_below_uses_ttft_of_agent_turns(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    for index, latency in enumerate([300, 400, 500, 1500]):
        log.say("user", "q", t_ms=index * 5000, end_ms=index * 5000 + 100)
        log.say("agent", "a", t_ms=index * 5000 + 100 + latency, latency_ms=latency)
    slow = await LatencyP95BelowGrader().grade(
        ctx_for(log, {"type": "latency_p95_below", "ms": 1000})
    )
    fast = await LatencyP95BelowGrader().grade(
        ctx_for(log, {"type": "latency_p95_below", "ms": 2000})
    )
    assert not slow.passed
    assert slow.value == 1500.0
    assert len(slow.evidence) == 1
    assert fast.passed


async def test_latency_is_skipped_without_measured_turns_or_on_email(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.say("agent", "hello")
    criterion = {"type": "latency_p95_below", "ms": 1000}
    assert (await LatencyP95BelowGrader().grade(ctx_for(log, criterion))).status == "skipped"
    email = ctx_for(log, criterion, channel=Channel.EMAIL)
    assert (await LatencyP95BelowGrader().grade(email)).status == "skipped"


async def test_wrong_criterion_type_is_a_wiring_bug(log: EventLog, ctx_for: ContextFactory) -> None:
    with pytest.raises(TypeError, match="ContainsCriterion"):
        await ContainsGrader().grade(ctx_for(log, {"type": "max_turns", "limit": 1}))
