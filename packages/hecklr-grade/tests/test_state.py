from __future__ import annotations

import copy
from typing import TYPE_CHECKING

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from hecklr_grade.state import (
    IsoDateTime,
    StateExpressionError,
    StateGrader,
    StateLookupError,
    check_expression,
    evaluate,
    wrap,
)

if TYPE_CHECKING:
    from conftest import AcmeData, ContextFactory, EventLog, SnapshotFactory

WORLD: dict[str, JsonValue] = {
    "appointments": {"C-1042": {"customer_id": "C-1042", "slot": "2026-10-27T09:00"}},
    "refunds": {},
    "items": {"a": 1},
    "customers": {"C-1042": {"name": "Camille", "tags": ["vip", "fr"]}},
}
BEFORE: dict[str, JsonValue] = {
    **WORLD,
    "appointments": {"C-1042": {"customer_id": "C-1042", "slot": "2026-10-23T09:00"}},
}
SPEC_EXPR = (
    "world.appointments['C-1042'].slot.weekday < 5 and world.appointments['C-1042'].slot.hour < 12"
)


def run(expr: str, *, after: dict[str, JsonValue] | None = None) -> bool:
    return evaluate(
        expr,
        world_after=WORLD if after is None else after,
        world_before=BEFORE,
        facts={"customer_id": "C-1042", "current_slot": "2026-10-23T09:00"},
    )


@pytest.mark.parametrize(
    "expr",
    [
        SPEC_EXPR,
        "world_after.appointments['C-1042'].slot == '2026-10-27T09:00'",
        "world.appointments['C-1042'].slot != world_before.appointments['C-1042'].slot",
        "world.appointments[facts.customer_id].slot.date == '2026-10-27'",
        "facts.current_slot.weekday == 4",
        "len(world.refunds) == 0 and not world.refunds",
        "world.items.a == 1",
        "'C-1042' in world.appointments",
        "all(a.slot.hour < 12 for a in values(world.appointments))",
        "keys(world.appointments) == ['C-1042']",
        "'vip' in world.customers['C-1042'].tags",
        "world.customers['C-1042'].name.lower().startswith('cam')",
        "world.customers == world_before.customers",
        "world.appointments['C-1042'].slot > world_before.appointments['C-1042'].slot",
    ],
)
def test_true_expressions(expr: str) -> None:
    assert run(expr) is True


def test_spec_example_fails_for_a_saturday_afternoon_slot() -> None:
    after: dict[str, JsonValue] = {"appointments": {"C-1042": {"slot": "2026-10-24T15:30"}}}
    assert run(SPEC_EXPR, after=after) is False


def test_iso_date_time_fields() -> None:
    slot = wrap("2026-10-23T09:15:30+02:00")
    assert isinstance(slot, IsoDateTime)
    assert slot == "2026-10-23T09:15:30+02:00"
    assert (slot.year, slot.month, slot.day) == (2026, 10, 23)
    assert (slot.hour, slot.minute, slot.second, slot.weekday) == (9, 15, 30, 4)  # a Friday
    assert slot.date == "2026-10-23"


@pytest.mark.parametrize("text", ["2026-13-01", "hello", "2026-10-23T25:00", "20261023"])
def test_non_dates_stay_plain_strings(text: str) -> None:
    assert type(wrap(text)) is str


@pytest.mark.parametrize(
    "expr",
    [
        "world.appointments['C-9999'].slot.hour < 12",
        "world.nope",
        "facts.missing == 1",
        "world.customers['C-1042'].tags[5] == 'x'",
        "world.customers['C-1042'].name.hour < 12",
        "world.items.a < 'x'",
        "1 / (len(world.refunds)) > 0",
        "keys(world.items.a) == []",
        "values(world.items.a) == []",
        "world.appointments[1] == 2",
    ],
)
def test_missing_or_mistyped_data_is_a_lookup_error(expr: str) -> None:
    with pytest.raises(StateLookupError):
        run(expr)


@pytest.mark.parametrize(
    "expr",
    [
        "().__class__.__bases__[0].__subclasses__()",
        "world.__class__",
        "world._data",
        "world.appointments['C-1042'].slot.__class__",
        "facts.current_slot._parsed",
        "'x'.format(1)",
        "str.mro()",
        "__import__('os').system('true')",
        "open('/etc/passwd')",
        "eval('1')",
        "exec('x=1')",
        "getattr(world, 'x')",
        "type(world)",
        "globals()",
        "lambda: True",
        "x = 1",
        "world.appointments['C-1042'].slot.isoformat()",
        "len",
        "len(world) + 1",
        "unknown_name",
        "if True",
        "",
        "1 if",
        "len(world.items) == 1; True",
    ],
)
def test_invalid_or_escaping_expressions_are_refused(expr: str) -> None:
    with pytest.raises(StateExpressionError):
        run(expr)


def test_check_expression_rejects_syntax_errors_only() -> None:
    check_expression(SPEC_EXPR)
    check_expression("unknown_name")
    with pytest.raises(StateExpressionError):
        check_expression("world.(")


ESCAPES = st.sampled_from(
    [
        "__class__",
        "__dict__",
        "__globals__",
        "__subclasses__",
        "__init__",
        "__builtins__",
        "__getattribute__",
        "__reduce__",
        "_data",
        "_parsed",
        "mro",
        "format",
        "format_map",
        "gi_frame",
        "func_globals",
        "f_locals",
        "items",
        "keys",
        "get",
        "pop",
        "update",
        "replace",
        "encode",
        "join",
        "split",
        "isoformat",
        "fromisoformat",
    ]
)
BASES = st.sampled_from(
    [
        "''",
        "()",
        "[]",
        "{}",
        "1",
        "len",
        "keys",
        "facts.current_slot",
        "world.customers['C-1042'].name",
        "world.customers['C-1042'].tags",
        "(lambda: 0)",
    ]
)


@settings(max_examples=300)
@given(BASES, st.lists(ESCAPES, min_size=1, max_size=3), st.sampled_from(["", "()", "[0]"]))
def test_attribute_escapes_never_succeed(base: str, chain: list[str], suffix: str) -> None:
    expr = base + "".join(f".{attr}" for attr in chain) + suffix
    with pytest.raises((StateExpressionError, StateLookupError)):
        run(expr)


@settings(max_examples=500)
@given(st.text(max_size=80))
def test_arbitrary_text_only_yields_a_bool_or_a_state_error(expr: str) -> None:
    world = copy.deepcopy(WORLD)
    try:
        result = evaluate(expr, world_after=world, world_before=BEFORE, facts={})
    except (StateExpressionError, StateLookupError):
        return
    finally:
        assert world == WORLD  # evaluation never mutates the snapshot
    assert isinstance(result, bool)


TOKENS = st.sampled_from(
    [
        "world",
        "world_before",
        "facts",
        ".",
        "appointments",
        "customers",
        "['C-1042']",
        ".slot",
        ".hour",
        ".weekday",
        " < ",
        " == ",
        " and ",
        " or ",
        " not ",
        "12",
        "'x'",
        "(",
        ")",
        "[",
        "]",
        "len(",
        "values(",
        "keys(",
        " for a in ",
        " if ",
        ", ",
        "__class__",
        "_data",
        "*",
        "**",
        "9",
    ]
)


@settings(max_examples=500)
@given(st.lists(TOKENS, max_size=12))
def test_token_soup_only_yields_a_bool_or_a_state_error(tokens: list[str]) -> None:
    world = copy.deepcopy(WORLD)
    try:
        result = evaluate("".join(tokens), world_after=world, world_before=BEFORE, facts={})
    except (StateExpressionError, StateLookupError):
        return
    finally:
        assert world == WORLD
    assert isinstance(result, bool)


def test_issue38_a_huge_non_boolean_integer_is_a_state_error() -> None:
    # Hypothesis found `12**121212`: its repr exceeds Python's 4300-digit int-to-str limit.
    with pytest.raises(StateExpressionError, match="must be a boolean, got int") as caught:
        run("12**121212")
    assert len(str(caught.value)) < 200


def test_issue38_a_long_non_boolean_result_is_described_briefly() -> None:
    with pytest.raises(StateExpressionError, match="got str 'xxx") as caught:
        run("'x' * 50000")
    assert len(str(caught.value)) < 200


def test_huge_powers_and_comprehensions_are_capped() -> None:
    with pytest.raises((StateExpressionError, StateLookupError)):
        run("9 ** 9 ** 9 ** 9 > 0")
    with pytest.raises(StateExpressionError):
        run("len([1 for a in 'x' * 99999 for b in 'y' * 99999]) > 0")


@pytest.mark.anyio
async def test_state_grader_pass_fail_and_error(
    log: EventLog, ctx_for: ContextFactory, acme: AcmeData, snap: SnapshotFactory
) -> None:
    deleted = snap({**acme, "appointments": {}})
    acme["appointments"]["C-1042"]["slot"] = "2026-10-27T09:00"
    after = snap(acme, t_ms=5000)
    grader = StateGrader()

    ok = await grader.grade(ctx_for(log, {"type": "state", "expr": SPEC_EXPR}, world_after=after))
    assert ok.passed
    assert ok.status == "ok"

    unmoved = await grader.grade(
        ctx_for(log, {"type": "state", "expr": "world.appointments['C-1042'].slot.weekday == 0"})
    )
    assert not unmoved.passed
    assert unmoved.status == "ok"

    missing = await grader.grade(
        ctx_for(log, {"type": "state", "expr": SPEC_EXPR}, world_after=deleted)
    )
    assert not missing.passed
    assert missing.status == "ok"
    assert "data missing" in (missing.rationale or "")

    # F2: the tool description swapped to MM/DD, so the stored slot is no longer ISO 8601.
    acme["appointments"]["C-1042"]["slot"] = "10/27/2026 09:00"
    f2 = await grader.grade(
        ctx_for(log, {"type": "state", "expr": SPEC_EXPR}, world_after=snap(acme))
    )
    assert not f2.passed
    assert f2.status == "ok"
    assert "not an ISO 8601 date-time" in (f2.rationale or "")

    bad = await grader.grade(ctx_for(log, {"type": "state", "expr": "world.__class__"}))
    assert bad.status == "error"
    assert not bad.passed
