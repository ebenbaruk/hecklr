from __future__ import annotations

from typing import TYPE_CHECKING

import pytest
from hypothesis import given
from hypothesis import strategies as st
from pydantic import JsonValue

from hecklr_grade.tools import (
    ArgsMatchGrader,
    CallOrderGrader,
    ToolCalledGrader,
    ToolNotCalledGrader,
    json_equal,
)

if TYPE_CHECKING:
    from conftest import ContextFactory, EventLog

pytestmark = pytest.mark.anyio

RESCHEDULE = {
    "type": "tool_called",
    "tool": "reschedule_appointment",
    "args": {"customer_id": "C-1042"},
}


async def test_tool_called_matches_an_args_subset(log: EventLog, ctx_for: ContextFactory) -> None:
    call, _ = log.call(
        "reschedule_appointment", {"customer_id": "C-1042", "slot": "2026-10-27T09:00"}
    )
    grade = await ToolCalledGrader().grade(ctx_for(log, RESCHEDULE))
    assert grade.passed
    assert grade.value == 1.0
    assert [ref.event_id for ref in grade.evidence] == [call.id]


async def test_tool_called_fails_on_wrong_args_and_cites_the_calls(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    call, _ = log.call("reschedule_appointment", {"customer_id": "C-2077"})
    log.call("get_customer", {"customer_id": "C-1042"})
    grade = await ToolCalledGrader().grade(ctx_for(log, RESCHEDULE))
    assert not grade.passed
    assert [ref.event_id for ref in grade.evidence] == [call.id]
    assert "0 of 1" in (grade.rationale or "")


async def test_tool_called_honours_min_calls(log: EventLog, ctx_for: ContextFactory) -> None:
    log.call("get_customer", {"customer_id": "C-1042"})
    criterion = {"type": "tool_called", "tool": "get_customer", "min_calls": 2}
    assert not (await ToolCalledGrader().grade(ctx_for(log, criterion))).passed
    log.call("get_customer", {"customer_id": "C-1042"})
    assert (await ToolCalledGrader().grade(ctx_for(log, criterion))).passed


async def test_tool_called_fails_when_never_called(log: EventLog, ctx_for: ContextFactory) -> None:
    log.say("agent", "Done!")
    grade = await ToolCalledGrader().grade(ctx_for(log, RESCHEDULE))
    assert not grade.passed
    assert grade.evidence == []


async def test_tool_not_called(log: EventLog, ctx_for: ContextFactory) -> None:
    criterion = {"type": "tool_not_called", "tool": "issue_refund"}
    log.call("get_customer", {"customer_id": "C-1042"})
    assert (await ToolNotCalledGrader().grade(ctx_for(log, criterion))).passed
    refund, _ = log.call("issue_refund", {"invoice_id": "INV-501"})
    grade = await ToolNotCalledGrader().grade(ctx_for(log, criterion))
    assert not grade.passed
    assert [ref.event_id for ref in grade.evidence] == [refund.id]


async def test_tool_not_called_with_args_only_forbids_matching_calls(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.call("get_customer", {"customer_id": "C-1042"})
    criterion = {
        "type": "tool_not_called",
        "tool": "get_customer",
        "args": {"customer_id": "C-2077"},
    }
    assert (await ToolNotCalledGrader().grade(ctx_for(log, criterion))).passed


async def test_args_match_checks_every_call(log: EventLog, ctx_for: ContextFactory) -> None:
    criterion = {
        "type": "args_match",
        "tool": "reschedule_appointment",
        "args": {"customer_id": "C-1042"},
        "patterns": {"slot": r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}"},
    }
    log.call("reschedule_appointment", {"customer_id": "C-1042", "slot": "2026-10-27T09:00"})
    assert (await ArgsMatchGrader().grade(ctx_for(log, criterion))).passed
    # F2: the swapped tool description makes the agent send MM/DD dates.
    bad, _ = log.call(
        "reschedule_appointment", {"customer_id": "C-1042", "slot": "10/27/2026 09:00"}
    )
    grade = await ArgsMatchGrader().grade(ctx_for(log, criterion))
    assert not grade.passed
    assert [ref.event_id for ref in grade.evidence] == [bad.id]
    assert "does not match" in (grade.rationale or "")


@pytest.mark.parametrize(
    ("args", "problem"),
    [
        ({"slot": "2026-10-27T09:00"}, "missing customer_id"),
        ({"customer_id": "C-9", "slot": "2026-10-27T09:00"}, "expected"),
        ({"customer_id": "C-1042"}, "missing slot"),
        ({"customer_id": "C-1042", "slot": 20261027}, "does not match"),
    ],
)
async def test_args_match_reports_the_problem(
    log: EventLog, ctx_for: ContextFactory, args: dict[str, JsonValue], problem: str
) -> None:
    criterion = {
        "type": "args_match",
        "tool": "reschedule_appointment",
        "args": {"customer_id": "C-1042"},
        "patterns": {"slot": r"\d{4}-\d{2}-\d{2}T.*"},
    }
    log.call("reschedule_appointment", args)
    grade = await ArgsMatchGrader().grade(ctx_for(log, criterion))
    assert not grade.passed
    assert problem in (grade.rationale or "")


async def test_args_match_is_skipped_when_the_tool_was_never_called(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    criterion = {"type": "args_match", "tool": "reschedule_appointment", "patterns": {"slot": ".*"}}
    grade = await ArgsMatchGrader().grade(ctx_for(log, criterion))
    assert grade.status == "skipped"
    assert not grade.passed


async def test_args_match_invalid_pattern_is_an_error(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.call("reschedule_appointment", {"slot": "x"})
    criterion = {"type": "args_match", "tool": "reschedule_appointment", "patterns": {"slot": "["}}
    assert (await ArgsMatchGrader().grade(ctx_for(log, criterion))).status == "error"


async def test_call_order_as_subsequence(log: EventLog, ctx_for: ContextFactory) -> None:
    verify, _ = log.call("verify_customer", t_ms=10)
    log.call("get_customer", t_ms=20)
    refund, _ = log.call("issue_refund", t_ms=30)
    criterion = {"type": "call_order", "tools": ["verify_customer", "issue_refund"]}
    grade = await CallOrderGrader().grade(ctx_for(log, criterion))
    assert grade.passed
    assert [ref.event_id for ref in grade.evidence] == [verify.id, refund.id]
    strict = {**criterion, "strict": True}
    assert not (await CallOrderGrader().grade(ctx_for(log, strict))).passed


async def test_call_order_uses_time_order_not_append_order(
    log: EventLog, ctx_for: ContextFactory
) -> None:
    log.call("issue_refund", t_ms=50)
    log.call("verify_customer", t_ms=10)
    criterion = {"type": "call_order", "tools": ["verify_customer", "issue_refund"], "strict": True}
    assert (await CallOrderGrader().grade(ctx_for(log, criterion))).passed


async def test_call_order_fails_when_reversed(log: EventLog, ctx_for: ContextFactory) -> None:
    log.call("issue_refund", t_ms=10)
    log.call("verify_customer", t_ms=20)
    criterion = {"type": "call_order", "tools": ["verify_customer", "issue_refund"]}
    grade = await CallOrderGrader().grade(ctx_for(log, criterion))
    assert not grade.passed
    assert "issue_refund -> verify_customer" in (grade.rationale or "")


def test_json_equal_distinguishes_bools_from_numbers() -> None:
    assert json_equal(1, 1.0)
    assert not json_equal(True, 1)
    assert json_equal({"a": [1, {"b": True}]}, {"a": [1.0, {"b": True}]})
    assert not json_equal({"a": 1}, {"a": 1, "b": 2})
    assert not json_equal([1], [1, 2])


json_values = st.recursive(
    st.none() | st.booleans() | st.integers() | st.text(max_size=5),
    lambda children: (
        st.lists(children, max_size=3) | st.dictionaries(st.text(max_size=3), children, max_size=3)
    ),
    max_leaves=10,
)


@given(json_values)
def test_json_equal_is_reflexive(value: JsonValue) -> None:
    assert json_equal(value, value)


@given(json_values, json_values)
def test_json_equal_is_symmetric(left: JsonValue, right: JsonValue) -> None:
    assert json_equal(left, right) == json_equal(right, left)
