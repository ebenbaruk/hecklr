"""Tool execution through the direct in-process API: every operation, errors and events."""

from collections.abc import Callable
from typing import Any

import pytest

from hecklr_schemas import ErrorPayload, TrialBrief, World, WorldSession
from hecklr_world import SessionClosedError, TrialWorld, WorldConfigError

BriefFactory = Callable[..., TrialBrief]
SinkFactory = Callable[..., Any]


@pytest.fixture
def brief(make_brief: BriefFactory) -> TrialBrief:
    return make_brief()


@pytest.fixture
def sink(brief: TrialBrief, make_sink: SinkFactory) -> Any:
    return make_sink(brief.trial_id)


@pytest.fixture
def trial(example_world: World, brief: TrialBrief, sink: Any) -> TrialWorld:
    return TrialWorld(brief, example_world, sink)


def test_trial_world_implements_the_world_session_protocol(trial: TrialWorld) -> None:
    assert isinstance(trial, WorldSession)
    assert trial.mcp_url is None
    assert trial.http_url is None
    assert [tool.name for tool in trial.tools][:2] == ["get_customer", "verify_customer"]


@pytest.mark.anyio
async def test_get_looks_up_a_customer_by_id(trial: TrialWorld) -> None:
    outcome = await trial.call_tool("get_customer", {"customer_id": "C-1042"})
    assert outcome.ok
    assert outcome.http_status == 200
    assert outcome.body() == {
        "customer_id": "C-1042",
        "name": "Camille Durand",
        "postcode": "92000",
        "address": "4 Rue Lepic",
    }


@pytest.mark.anyio
async def test_find_verifies_identity_or_reports_not_found(trial: TrialWorld) -> None:
    ok = await trial.call_tool("verify_customer", {"customer_id": "C-1042", "postcode": "92000"})
    bad = await trial.call_tool("verify_customer", {"customer_id": "C-1042", "postcode": "75011"})
    assert ok.ok
    assert bad.error is not None
    assert bad.http_status == 404
    assert bad.body() == {
        "error": {
            "code": "not_found",
            "message": "no customers record matches customer_id='C-1042', postcode='75011'",
        }
    }


@pytest.mark.anyio
async def test_list_filters_records_by_the_supplied_fields(trial: TrialWorld) -> None:
    outcome = await trial.call_tool("list_appointments", {"customer_id": "C-1042"})
    assert isinstance(outcome.result, list)
    ids = [record["appointment_id"] for record in outcome.result if isinstance(record, dict)]
    assert ids == ["A-1", "A-2"]


@pytest.mark.anyio
async def test_update_reschedules_and_cancels_appointments(trial: TrialWorld) -> None:
    moved = await trial.call_tool(
        "reschedule_appointment", {"appointment_id": "A-1", "slot": "2026-10-28T09:00"}
    )
    cancelled = await trial.call_tool(
        "cancel_appointment", {"appointment_id": "A-2", "status": "cancelled"}
    )
    state = trial.snapshot().collections["appointments"]
    assert moved.ok
    assert cancelled.ok
    assert state["A-1"]["slot"] == "2026-10-28T09:00"
    assert state["A-2"]["status"] == "cancelled"
    assert state["A-3"]["slot"] == "2026-10-24T10:00"


@pytest.mark.anyio
async def test_update_is_refused_when_the_record_schema_rejects_it(trial: TrialWorld) -> None:
    outcome = await trial.call_tool(
        "reschedule_appointment", {"appointment_id": "A-1", "slot": "10/28/2026 9am"}
    )
    assert outcome.error is not None
    assert outcome.error.code == "invalid_arguments"
    assert trial.snapshot().collections["appointments"]["A-1"]["slot"] == "2026-10-23T09:00"


@pytest.mark.anyio
async def test_create_issues_a_refund_with_a_generated_deterministic_key(trial: TrialWorld) -> None:
    first = await trial.call_tool("issue_refund", {"invoice_id": "INV-501", "amount_eur": 40})
    second = await trial.call_tool("issue_refund", {"invoice_id": "INV-501", "amount_eur": 9.5})
    assert first.result == {"invoice_id": "INV-501", "amount_eur": 40, "refund_id": "refunds-1"}
    assert second.result == {"invoice_id": "INV-501", "amount_eur": 9.5, "refund_id": "refunds-2"}
    assert sorted(trial.snapshot().collections["refunds"]) == ["refunds-1", "refunds-2"]


@pytest.mark.anyio
async def test_delete_uses_the_key_param_and_returns_the_record(trial: TrialWorld) -> None:
    await trial.call_tool("issue_refund", {"invoice_id": "INV-501", "amount_eur": 40})
    deleted = await trial.call_tool("delete_refund", {"id": "refunds-1"})
    again = await trial.call_tool("delete_refund", {"id": "refunds-1"})
    assert deleted.ok
    assert again.http_status == 404
    assert trial.snapshot().collections["refunds"] == {}


@pytest.mark.anyio
async def test_get_policy_reads_text_from_the_seed_file(trial: TrialWorld) -> None:
    outcome = await trial.call_tool("get_policy", {"policy_id": "cancellation"})
    assert outcome.result == {
        "policy_id": "cancellation",
        "text": "Cancellations are free up to 24 hours before the visit.",
    }


@pytest.mark.anyio
async def test_python_tool_creates_an_emergency_handoff(trial: TrialWorld) -> None:
    outcome = await trial.call_tool(
        "escalate_emergency", {"customer_id": "C-1042", "reason": "gas smell in the kitchen"}
    )
    assert outcome.result == {
        "customer_id": "C-1042",
        "reason": "gas smell in the kitchen",
        "urgent": True,
        "handoff_id": "handoffs-1",
    }


@pytest.mark.anyio
async def test_python_tool_errors_reach_the_agent_as_structured_errors(trial: TrialWorld) -> None:
    unknown = await trial.call_tool("escalate_emergency", {"customer_id": "C-9", "reason": "gas"})
    blank = await trial.call_tool("escalate_emergency", {"customer_id": "C-1042", "reason": " "})
    assert unknown.body() == {
        "error": {"code": "not_found", "message": "no customers record with customer_id='C-9'"}
    }
    assert blank.body() == {
        "error": {"code": "tool_failed", "message": "an emergency needs a reason"}
    }


@pytest.mark.anyio
@pytest.mark.parametrize(
    ("args", "message"),
    [
        ({}, "get_customer: 'customer_id' is a required property"),
        ({"customer_id": 1042}, "get_customer: customer_id: 1042 is not of type 'string'"),
        (
            {"customer_id": "C-1042", "pin": "0000"},
            "get_customer: Additional properties are not allowed ('pin' was unexpected)",
        ),
    ],
)
async def test_arguments_are_validated_against_the_tool_schema(
    trial: TrialWorld, args: dict[str, Any], message: str
) -> None:
    outcome = await trial.call_tool("get_customer", args)
    assert outcome.http_status == 400
    assert outcome.body() == {"error": {"code": "invalid_arguments", "message": message}}


@pytest.mark.anyio
async def test_unknown_tool_is_reported_and_still_recorded(trial: TrialWorld, sink: Any) -> None:
    outcome = await trial.call_tool("drop_tables", {})
    assert outcome.http_status == 404
    assert outcome.body() == {
        "error": {"code": "unknown_tool", "message": "unknown tool 'drop_tables'"}
    }
    assert [c.tool for c in sink.calls()] == ["drop_tables"]
    assert sink.results()[0].error == "unknown_tool: unknown tool 'drop_tables'"


@pytest.mark.anyio
async def test_every_call_emits_a_tool_call_and_a_tool_result(trial: TrialWorld, sink: Any) -> None:
    sink.clock.advance(1_000)
    outcome = await trial.call_tool("get_customer", {"customer_id": "C-2077"})
    call, result = sink.events
    assert (call.actor, call.t_ms) == ("agent", 1_000)
    assert call.payload.kind == "tool_call"
    assert call.payload.call_id == outcome.call_id
    assert call.payload.args == {"customer_id": "C-2077"}
    assert call.payload.source == "sandbox"
    assert result.actor == "tool"
    assert result.payload.kind == "tool_result"
    assert result.payload.call_id == outcome.call_id
    assert result.payload.result == outcome.result
    assert (result.payload.latency_ms, result.payload.fault, result.payload.error) == (
        0,
        None,
        None,
    )


@pytest.mark.anyio
async def test_a_crashing_python_tool_is_a_500_and_an_error_event(
    example_world: World, brief: TrialBrief, sink: Any
) -> None:
    world = _with_python_tool(example_world, "crash", "example_tools:crash")
    trial = TrialWorld(brief, world, sink)
    outcome = await trial.call_tool("crash", {})
    assert outcome.http_status == 500
    assert outcome.body() == {
        "error": {"code": "internal_error", "message": "Internal Server Error"}
    }
    errors = [e.payload for e in sink.events if isinstance(e.payload, ErrorPayload)]
    assert len(errors) == 1
    assert errors[0].source == "world"
    assert errors[0].message == "tool 'crash' crashed: ZeroDivisionError: division by zero"


@pytest.mark.anyio
async def test_a_python_tool_returning_non_json_is_a_500_with_both_events(
    example_world: World, brief: TrialBrief, sink: Any
) -> None:
    world = _with_python_tool(example_world, "when", "example_tools:returns_a_datetime")
    trial = TrialWorld(brief, world, sink)
    outcome = await trial.call_tool("when", {})
    assert outcome.http_status == 500
    assert outcome.body() == {
        "error": {"code": "internal_error", "message": "Internal Server Error"}
    }
    [call] = sink.calls()
    [result] = sink.results()
    assert result.call_id == call.call_id
    assert result.error == "internal_error: Internal Server Error"
    [error] = [e.payload for e in sink.events if isinstance(e.payload, ErrorPayload)]
    assert error.source == "world"
    assert error.message.startswith("tool 'when' crashed: returned a value that is not JSON")


@pytest.mark.anyio
async def test_a_python_tool_raising_a_timeout_records_its_result(
    example_world: World, brief: TrialBrief, sink: Any
) -> None:
    world = _with_python_tool(example_world, "slow_upstream", "example_tools:upstream_timeout")
    trial = TrialWorld(brief, world, sink)
    outcome = await trial.call_tool("slow_upstream", {})
    assert outcome.http_status == 504
    assert outcome.body() == {"error": {"code": "timeout", "message": "upstream timed out"}}
    [call] = sink.calls()
    [result] = sink.results()
    assert (result.call_id, result.error, result.fault) == (
        call.call_id,
        "timeout: upstream timed out",
        None,
    )


@pytest.mark.anyio
async def test_a_python_tool_may_raise_its_own_tool_errors(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    world = _with_python_tool(example_world, "nothing", "example_tools:missing_record")
    brief = make_brief()
    trial = TrialWorld(brief, world, make_sink(brief.trial_id))
    outcome = await trial.call_tool("nothing", {})
    assert outcome.body() == {"error": {"code": "not_found", "message": "nothing here"}}


@pytest.mark.parametrize(
    ("entrypoint", "message"),
    [
        ("no_such_module:tool", "cannot import tool module 'no_such_module'"),
        ("example_tools:absent", "tool entry point 'example_tools:absent' is not a callable"),
        ("example_tools:not_callable", "'example_tools:not_callable' is not a callable"),
    ],
)
def test_bad_python_entry_points_fail_when_the_world_opens(
    example_world: World,
    make_brief: BriefFactory,
    make_sink: SinkFactory,
    entrypoint: str,
    message: str,
) -> None:
    world = _with_python_tool(example_world, "broken", entrypoint)
    brief = make_brief()
    with pytest.raises(WorldConfigError, match=message):
        TrialWorld(brief, world, make_sink(brief.trial_id))


def test_an_invalid_params_schema_fails_when_the_world_opens(
    example_world: World, make_brief: BriefFactory, make_sink: SinkFactory
) -> None:
    data = example_world.model_dump()
    data["tools"][0]["params"]["required"] = "customer_id"
    brief = make_brief()
    with pytest.raises(WorldConfigError, match="tool 'get_customer': invalid params schema"):
        TrialWorld(brief, World.model_validate(data), make_sink(brief.trial_id))


@pytest.mark.anyio
async def test_a_closed_world_refuses_calls(trial: TrialWorld) -> None:
    await trial.close()
    await trial.close()
    assert trial.closed
    with pytest.raises(SessionClosedError):
        await trial.call_tool("get_customer", {"customer_id": "C-1042"})


def _with_python_tool(world: World, name: str, entrypoint: str) -> World:
    data = world.model_dump()
    data["tools"].append(
        {"name": name, "description": name, "op": {"kind": "python", "entrypoint": entrypoint}}
    )
    return World.model_validate(data)
