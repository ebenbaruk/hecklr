"""Fault injection (Scenario.faults): every kind, seeded and deterministic."""

from collections.abc import Callable
from typing import Any

import anyio
import pytest

from hecklr_schemas import FaultKind, MonotonicClock, ToolFault, TrialBrief, World
from hecklr_world import TrialWorld, WorldConfigError
from hecklr_world.faults import FaultPlan, apply_stale_patch

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]

GET = {"customer_id": "C-1042"}


def _open(world: World, brief: TrialBrief, sink: Any) -> TrialWorld:
    return TrialWorld(brief, world, sink)


@pytest.mark.anyio
async def test_http_500_fails_the_call_without_touching_state(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "reschedule_appointment", "kind": "http_500"}])
    sink = make_sink(brief.trial_id)
    trial = _open(example_world, brief, sink)
    outcome = await trial.call_tool(
        "reschedule_appointment", {"appointment_id": "A-1", "slot": "2026-10-28T09:00"}
    )
    assert outcome.http_status == 500
    assert outcome.fault is FaultKind.HTTP_500
    assert trial.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-23T09:00"
    result = sink.results()[0]
    assert (result.fault, result.error) == (
        FaultKind.HTTP_500,
        "internal_error: Internal Server Error",
    )


@pytest.mark.anyio
async def test_slow_delays_the_reply_and_records_the_latency(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "slow", "delay_ms": 120}])
    sink = make_sink(brief.trial_id, MonotonicClock())
    trial = _open(example_world, brief, sink)
    outcome = await trial.call_tool("get_customer", GET)
    assert outcome.ok
    assert outcome.fault is FaultKind.SLOW
    result = sink.results()[0]
    assert result.fault is FaultKind.SLOW
    assert 120 <= result.latency_ms < 1_000


@pytest.mark.anyio
async def test_stale_data_patches_returned_records_but_not_the_store(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    patch = {"slot": "2026-10-01T08:00"}
    brief = make_brief(faults=[{"tool": "list_appointments", "kind": "stale_data", "patch": patch}])
    trial = _open(example_world, brief, make_sink(brief.trial_id))
    outcome = await trial.call_tool("list_appointments", {"customer_id": "C-1042"})
    assert isinstance(outcome.result, list)
    assert {r["slot"] for r in outcome.result if isinstance(r, dict)} == {"2026-10-01T08:00"}
    assert outcome.fault is FaultKind.STALE_DATA
    assert trial.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-23T09:00"


@pytest.mark.anyio
async def test_timeout_holds_the_call_until_the_trial_ends(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "timeout"}])
    sink = make_sink(brief.trial_id)
    trial = _open(example_world, brief, sink)
    outcomes: list[Any] = []

    async def call() -> None:
        outcomes.append(await trial.call_tool("get_customer", GET))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        await anyio.wait_all_tasks_blocked()
        assert outcomes == []
        assert len(sink.calls()) == 1
        assert sink.results() == []
        sink.clock.advance(30_000)
        await trial.close()

    assert outcomes[0].http_status == 504
    assert outcomes[0].fault is FaultKind.TIMEOUT
    [result] = sink.results()
    assert result.fault is FaultKind.TIMEOUT
    assert result.latency_ms == 30_000
    assert result.error == "timeout: no response before the trial ended"


@pytest.mark.anyio
async def test_a_slow_call_in_flight_at_close_is_recorded_and_never_writes(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(
        faults=[{"tool": "reschedule_appointment", "kind": "slow", "delay_ms": 60_000}]
    )
    sink = make_sink(brief.trial_id)
    trial = _open(example_world, brief, sink)
    outcomes: list[Any] = []
    args = {"appointment_id": "A-1", "slot": "2026-10-28T09:00"}

    async def call() -> None:
        outcomes.append(await trial.call_tool("reschedule_appointment", args))

    async with anyio.create_task_group() as tg:
        tg.start_soon(call)
        await anyio.wait_all_tasks_blocked()
        sink.clock.advance(5_000)
        await trial.close()
        # Recorded by close() itself, so before the caller's "after" snapshot.
        [result] = sink.results()
        assert (result.fault, result.latency_ms) == (FaultKind.SLOW, 5_000)
        assert result.error == "timeout: no response before the trial ended"

    assert outcomes[0].http_status == 504
    assert len(sink.results()) == 1
    assert trial.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-23T09:00"


@pytest.mark.anyio
async def test_a_held_call_cancelled_right_after_close_is_recorded_once(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "timeout"}])
    sink = make_sink(brief.trial_id)
    trial = _open(example_world, brief, sink)
    async with anyio.create_task_group() as tg:
        tg.start_soon(trial.call_tool, "get_customer", GET)
        await anyio.wait_all_tasks_blocked()
        await trial.close()
        # The server shutting down cancels the waiting request before it resumes.
        tg.cancel_scope.cancel()
    [result] = sink.results()
    assert result.error == "timeout: no response before the trial ended"


@pytest.mark.anyio
async def test_a_cancelled_held_call_is_still_recorded_once(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "timeout"}])
    sink = make_sink(brief.trial_id)
    trial = _open(example_world, brief, sink)
    with anyio.move_on_after(0.05):
        await trial.call_tool("get_customer", GET)
    await trial.close()
    [result] = sink.results()
    assert result.error == "timeout: the call was abandoned before it completed"


@pytest.mark.anyio
async def test_after_calls_lets_the_first_calls_succeed(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "get_customer", "kind": "http_500", "after_calls": 2}])
    trial = _open(example_world, brief, make_sink(brief.trial_id))
    statuses = [(await trial.call_tool("get_customer", GET)).http_status for _ in range(4)]
    assert statuses == [200, 200, 500, 500]


def _schedule(seed: int, probability: float, calls: int = 200) -> list[bool]:
    fault = ToolFault(tool="t", kind=FaultKind.HTTP_500, probability=probability)
    plan = FaultPlan([fault], seed=seed, tools=["t", "u"])
    return [plan.next_fault("t") is not None for _ in range(calls)]


def test_fault_schedule_is_a_pure_function_of_the_seed() -> None:
    assert _schedule(seed=1, probability=0.3) == _schedule(seed=1, probability=0.3)
    assert _schedule(seed=1, probability=0.3) != _schedule(seed=2, probability=0.3)


def test_fault_probability_is_respected() -> None:
    assert not any(_schedule(seed=3, probability=0.0))
    assert all(_schedule(seed=3, probability=1.0))
    rate = sum(_schedule(seed=3, probability=0.3, calls=4_000)) / 4_000
    assert 0.27 < rate < 0.33


def test_calls_to_other_tools_do_not_shift_a_tools_schedule() -> None:
    fault = ToolFault(tool="t", kind=FaultKind.HTTP_500, probability=0.5)
    alone = FaultPlan([fault], seed=9, tools=["t", "u"])
    interleaved = FaultPlan([fault], seed=9, tools=["t", "u"])
    expected = [alone.next_fault("t") for _ in range(50)]
    seen = []
    for _ in range(50):
        assert interleaved.next_fault("u") is None
        seen.append(interleaved.next_fault("t"))
    assert seen == expected


def test_the_first_triggered_fault_in_list_order_wins() -> None:
    slow = ToolFault(tool="t", kind=FaultKind.SLOW, delay_ms=5)
    broken = ToolFault(tool="t", kind=FaultKind.HTTP_500)
    plan = FaultPlan([slow, broken], seed=0, tools=["t"])
    assert plan.next_fault("t") is slow


def test_a_fault_on_an_unknown_tool_fails_when_the_world_opens(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    brief = make_brief(faults=[{"tool": "teleport", "kind": "http_500"}])
    with pytest.raises(WorldConfigError, match="fault targets unknown tool 'teleport'"):
        _open(example_world, brief, make_sink(brief.trial_id))


def test_stale_patch_only_applies_to_records() -> None:
    assert apply_stale_patch("text", {"a": 1}) == "text"
    assert apply_stale_patch([{"a": 0}, 3], {"a": 1}) == [{"a": 1}, 3]
