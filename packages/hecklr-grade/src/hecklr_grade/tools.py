"""Tool graders: what the agent did through its tools, read from ``tool_call`` events."""

import json
import re
from collections.abc import Mapping

from pydantic import JsonValue

from hecklr_grade.base import criterion_as, errored, make_grade, of_kind, skipped
from hecklr_schemas import Event, Grade, GradeContext, ToolCallPayload
from hecklr_schemas.scenario import (
    ArgsMatchCriterion,
    CallOrderCriterion,
    ToolCalledCriterion,
    ToolNotCalledCriterion,
)


def json_equal(left: JsonValue, right: JsonValue) -> bool:
    """JSON equality: ``1 == 1.0`` as in JSON, but ``true`` never equals ``1``."""
    if isinstance(left, bool) or isinstance(right, bool):
        return type(left) is type(right) and left == right
    if isinstance(left, dict) and isinstance(right, dict):
        return left.keys() == right.keys() and all(json_equal(left[k], right[k]) for k in left)
    if isinstance(left, list) and isinstance(right, list):
        return len(left) == len(right) and all(map(json_equal, left, right))
    return left == right


def args_contain(args: Mapping[str, JsonValue], expected: Mapping[str, JsonValue]) -> bool:
    """Every expected argument is present in ``args`` with a JSON-equal value."""
    return all(name in args and json_equal(args[name], value) for name, value in expected.items())


def calls_to(ctx: GradeContext, tool: str) -> list[tuple[Event, ToolCallPayload]]:
    """The trial's calls to ``tool``, in time order."""
    return [
        (event, call) for event, call in of_kind(ctx.events, ToolCallPayload) if call.tool == tool
    ]


def _describe(args: Mapping[str, JsonValue]) -> str:
    return f" with {json.dumps(args, sort_keys=True)}" if args else ""


class ToolCalledGrader:
    """``tool_called``: at least ``min_calls`` calls whose arguments contain ``args``."""

    name = "tool_called"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Count matching calls; the value is that count."""
        criterion = criterion_as(ctx, ToolCalledCriterion)
        calls = calls_to(ctx, criterion.tool)
        matching = [event for event, call in calls if args_contain(call.args, criterion.args)]
        return make_grade(
            ctx,
            self,
            passed=len(matching) >= criterion.min_calls,
            value=float(len(matching)),
            rationale=(
                f"{len(matching)} of {len(calls)} call(s) to {criterion.tool}"
                f"{_describe(criterion.args)} matched; needed {criterion.min_calls}"
            ),
            # On a miss, the calls with the wrong arguments explain why.
            evidence=matching or [event for event, _ in calls],
        )


class ToolNotCalledGrader:
    """``tool_not_called``: no call to the tool whose arguments contain ``args``."""

    name = "tool_not_called"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Fail on any matching call, citing every one."""
        criterion = criterion_as(ctx, ToolNotCalledCriterion)
        matching = [
            event
            for event, call in calls_to(ctx, criterion.tool)
            if args_contain(call.args, criterion.args)
        ]
        return make_grade(
            ctx,
            self,
            passed=not matching,
            rationale=(
                f"{len(matching)} forbidden call(s) to {criterion.tool}{_describe(criterion.args)}"
            ),
            evidence=matching,
        )


def _string_form(value: JsonValue) -> str:
    return value if isinstance(value, str) else json.dumps(value)


class ArgsMatchGrader:
    """``args_match``: every call to the tool has the exact ``args`` and matches ``patterns``.

    Skipped when the tool was never called: whether it should have been is ``tool_called``'s
    question, and a vacuous pass would hide that nothing was checked.
    """

    name = "args_match"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Fail on the calls with a wrong or missing argument, citing them."""
        criterion = criterion_as(ctx, ArgsMatchCriterion)
        try:
            patterns = {name: re.compile(regex) for name, regex in criterion.patterns.items()}
        except re.error as exc:
            return errored(ctx, self, f"invalid pattern: {exc}")
        calls = calls_to(ctx, criterion.tool)
        if not calls:
            return skipped(ctx, self, f"{criterion.tool} was never called")
        bad: list[Event] = []
        problems: list[str] = []
        for event, call in calls:
            problem = _first_problem(call.args, criterion.args, patterns)
            if problem is not None:
                bad.append(event)
                problems.append(f"{call.call_id}: {problem}")
        return make_grade(
            ctx,
            self,
            passed=not bad,
            rationale=(
                f"{len(calls) - len(bad)} of {len(calls)} call(s) to {criterion.tool} well-formed"
                + (f"; {'; '.join(problems)}" if problems else "")
            ),
            evidence=bad,
        )


def _first_problem(
    args: Mapping[str, JsonValue],
    expected: Mapping[str, JsonValue],
    patterns: Mapping[str, re.Pattern[str]],
) -> str | None:
    for name, value in expected.items():
        if name not in args:
            return f"missing {name}"
        if not json_equal(args[name], value):
            return f"{name}={json.dumps(args[name])}, expected {json.dumps(value)}"
    for name, pattern in patterns.items():
        if name not in args:
            return f"missing {name}"
        if pattern.fullmatch(_string_form(args[name])) is None:
            return f"{name}={_string_form(args[name])!r} does not match /{pattern.pattern}/"
    return None


class CallOrderGrader:
    """``call_order``: the tools were called in order, as a subsequence or (``strict``) adjacent."""

    name = "call_order"
    version = "1"

    async def grade(self, ctx: GradeContext) -> Grade:
        """Match the expected order against the sequence of all tool calls."""
        criterion = criterion_as(ctx, CallOrderCriterion)
        calls = of_kind(ctx.events, ToolCallPayload)
        names = [call.tool for _, call in calls]
        matched = (
            _contiguous_at(names, criterion.tools)
            if criterion.strict
            else _subsequence_at(names, criterion.tools)
        )
        mode = "contiguously" if criterion.strict else "in order"
        return make_grade(
            ctx,
            self,
            passed=matched is not None,
            rationale=(
                f"expected {' -> '.join(criterion.tools)} {mode}; "
                f"called {' -> '.join(names) or 'nothing'}"
            ),
            evidence=[calls[index][0] for index in matched or []],
        )


def _subsequence_at(names: list[str], wanted: list[str]) -> list[int] | None:
    """Indexes of the earliest occurrence of ``wanted`` as a subsequence of ``names``."""
    positions: list[int] = []
    for index, name in enumerate(names):
        if len(positions) < len(wanted) and name == wanted[len(positions)]:
            positions.append(index)
    return positions if len(positions) == len(wanted) else None


def _contiguous_at(names: list[str], wanted: list[str]) -> list[int] | None:
    """Indexes of the first run of ``names`` equal to ``wanted``."""
    for start in range(len(names) - len(wanted) + 1):
        if names[start : start + len(wanted)] == wanted:
            return list(range(start, start + len(wanted)))
    return None
