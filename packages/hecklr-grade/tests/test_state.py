from __future__ import annotations

import copy
import time
from typing import TYPE_CHECKING

import anyio
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from hecklr_grade.state import (
    MAX_POWER_BITS,
    MAX_VALUE_SIZE,
    IsoDateTime,
    StateExpressionError,
    StateExpressionTimeoutError,
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
    # Hypothesis found `12**121212`: its repr exceeds Python's 4300-digit int-to-str limit. That
    # power is now refused outright (#114); a product of two shifts still makes such an int.
    with pytest.raises(StateExpressionError, match="must be a boolean, got int") as caught:
        run("(1 << 10000) * (1 << 10000)")
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


# --- issue #114: a state expression is bounded in time and never blocks the event loop ----------

ISSUE114_EXPR = "len([9**4000000 for x in [0]*9999]) > 0"
MANY_STEPS = "len([x for x in [0] * 9000]) > 0"


def test_issue114_the_issue_expression_is_refused_at_once() -> None:
    started = time.monotonic()
    with pytest.raises(StateExpressionError, match="too high"):
        run(ISSUE114_EXPR)
    assert time.monotonic() - started < 0.5


@pytest.mark.parametrize(
    "expr",
    ["9**4000000 > 0", f"2**{MAX_POWER_BITS + 1} > 0", "(-3)**100000 > 0", "12**121212 > 0"],
)
def test_issue114_powers_past_the_result_bound_are_refused(expr: str) -> None:
    with pytest.raises(StateExpressionError, match="too high"):
        run(expr)


@pytest.mark.parametrize(
    "expr",
    [f"2**{MAX_POWER_BITS} > 0", "10**100 > 0", "2**-3 == 0.125", "1.5**2 == 2.25", "0**0 == 1"],
)
def test_issue114_powers_within_the_bound_still_work(expr: str) -> None:
    assert run(expr) is True


def test_issue114_an_expression_past_its_deadline_is_a_timeout() -> None:
    # Each step is fast, but there are enough of them to pass a deadline of zero.
    with pytest.raises(StateExpressionTimeoutError, match=r"exceeded its 0.0 s deadline"):
        evaluate(MANY_STEPS, world_after=WORLD, world_before=BEFORE, facts={}, deadline_s=0.0)
    assert issubclass(StateExpressionTimeoutError, StateExpressionError)


@pytest.mark.anyio
async def test_issue114_a_runaway_expression_is_an_error_grade_for_its_criterion(
    log: EventLog, ctx_for: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    issue = await StateGrader().grade(ctx_for(log, {"type": "state", "expr": ISSUE114_EXPR}))
    assert issue.status == "error"
    assert "too high" in (issue.rationale or "")

    monkeypatch.setattr("hecklr_grade.state.STATE_DEADLINE_S", 0.0)
    grade = await StateGrader().grade(ctx_for(log, {"type": "state", "expr": MANY_STEPS}))
    assert grade.status == "error"
    assert not grade.passed
    assert "deadline" in (grade.rationale or "")


def _slow_evaluate(expr: str, **kwargs: object) -> bool:
    time.sleep(0.6)  # a long evaluation that, like a real one, holds nothing the loop needs
    return True


@pytest.mark.anyio
async def test_issue114_grading_runs_off_the_event_loop(
    log: EventLog, ctx_for: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hecklr_grade.state.evaluate", _slow_evaluate)
    ticks = 0

    async def tick() -> None:
        nonlocal ticks
        while True:
            await anyio.sleep(0.01)
            ticks += 1

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(tick)
        grade = await StateGrader().grade(ctx_for(log, {"type": "state", "expr": "True"}))
        tasks.cancel_scope.cancel()
    assert grade.passed
    assert ticks >= 20, ticks  # the loop kept running while the expression was evaluated


@pytest.mark.anyio
async def test_issue114_a_cancelled_grade_returns_at_once(
    log: EventLog, ctx_for: ContextFactory, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ctrl-C cancels the run's tasks; a grade in progress must not hold the cancellation up.
    monkeypatch.setattr("hecklr_grade.state.evaluate", _slow_evaluate)
    started = time.monotonic()
    with anyio.move_on_after(0.05):
        await StateGrader().grade(ctx_for(log, {"type": "state", "expr": "True"}))
    assert time.monotonic() - started < 0.4


# One C-level operation (str, sorted, ==) on a value aliased many times over runs for seconds while
# holding the GIL: neither the deadline nor the worker thread can stop it, so such values are
# refused when they are built (#114).
ORDERS_WORLD: dict[str, JsonValue] = {
    "orders": {str(n): {"id": n, "note": "x" * 100} for n in range(1000)}
}
HUGE_VALUES = [
    "len(str([[0] * 9999] * 9999)) > 0",
    "sorted([[0] * 9999] * 9999) == []",
    "[[0] * 9999] * 9999 == [[0] * 9999] * 9999",
    "len(str(([0] * 9999,) * 9999)) > 0",
    "len(f'{[[0] * 9999] * 9999}') > 0",
    "len([world for x in [0] * 9999]) > 0",
]


@pytest.mark.parametrize("expr", HUGE_VALUES)
def test_issue114_values_too_large_to_handle_quickly_are_refused(expr: str) -> None:
    started = time.monotonic()
    with pytest.raises(StateExpressionError, match="too large"):
        run(expr, after=ORDERS_WORLD)
    assert time.monotonic() - started < 0.5


def test_issue114_a_value_built_piece_by_piece_stops_at_the_deadline() -> None:
    # Each piece is small and new, so no single node is too large; the deadline ends it.
    started = time.monotonic()
    with pytest.raises(StateExpressionTimeoutError):
        run("len(str([[0] * 9999 for x in [0] * 9999])) > 0")
    assert time.monotonic() - started < 1.5


@pytest.mark.parametrize(
    "expr",
    [
        f"len([[0] * 1000] * {MAX_VALUE_SIZE // 2000}) > 0",
        "len([o for o in values(world.orders) if o.id > 10]) == 989",
        "sorted(keys(world.orders))[0] == '0'",
        "len(str(world.orders)) > 0",
    ],
)
def test_issue114_values_within_the_size_bound_still_work(expr: str) -> None:
    assert run(expr, after=ORDERS_WORLD) is True


def test_issue114_sum_adds_numbers_only() -> None:
    # sum(lists, []) concatenates quadratically; numbers are what assertions add up.
    assert run("sum([1, 2, 3]) == 6") is True
    with pytest.raises(StateExpressionError):
        run("sum([[0] * 999] * 999, []) == []")
