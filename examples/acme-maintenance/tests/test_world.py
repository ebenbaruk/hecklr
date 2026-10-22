"""The acme-maintenance world: it loads in hecklr-world, and its data supports the suite."""

from datetime import datetime, timedelta

import pytest
from acme_agent.config import TODAY
from acme_agent.tools import LocalWorldTools
from acme_testing import files_for
from pydantic import JsonValue

from hecklr_schemas import World
from hecklr_sim import CROSS_CUSTOMER_DATA_PROBE
from hecklr_world import Store


def test_world_loads_into_a_hecklr_world_store(world: World) -> None:
    state = Store(world).state()
    assert world.id == "acme-maintenance"
    assert len(state["customers"]) == 12
    assert len(state["slots"]) == 34
    assert state["refunds"] == {}
    assert state["emergency_tickets"] == {}


def test_world_uses_only_declarative_tools(world: World) -> None:
    # A PythonOp would need this directory on sys.path inside the `hecklr` process.
    assert {tool.op.kind for tool in world.tools} <= {"get", "list", "find", "create", "update"}


@pytest.mark.anyio
async def test_every_tool_runs_with_the_arguments_the_catalog_documents(world: World) -> None:
    tools = LocalWorldTools(world)
    calls: dict[str, dict[str, JsonValue]] = {
        "get_customer": {"customer_id": "C-1042"},
        "verify_customer": {"customer_id": "C-1042", "postcode": "92000"},
        "list_appointments": {"customer_id": "C-1042"},
        "find_slots": {"weekday": "Monday", "period": "morning"},
        "book_appointment": {
            "customer_id": "C-1042",
            "service": "plumbing",
            "slot": "2026-10-26T09:00",
            "status": "booked",
        },
        "reschedule_appointment": {"appointment_id": "A-3001", "slot": "2026-10-27T09:00"},
        "cancel_appointment": {"appointment_id": "A-3012", "status": "cancelled"},
        "get_invoice": {"invoice_id": "INV-20481"},
        "list_invoices": {"customer_id": "C-1042"},
        "issue_refund": {
            "invoice_id": "INV-20482",
            "customer_id": "C-1042",
            "amount_eur": 89.0,
            "reason": "charged twice",
        },
        "create_emergency_ticket": {"category": "gas", "description": "smell of gas"},
        "get_policy": {"policy_id": "cancellation"},
    }
    assert set(calls) == {tool.name for tool in world.tools}
    for name, args in calls.items():
        outcome = await tools.call(name, args)
        assert outcome.ok, (name, outcome.body)
    state = tools.state()
    assert state["appointments"]["A-3001"]["slot"] == "2026-10-27T09:00"
    assert state["refunds"]["refunds-1"]["invoice_id"] == "INV-20482"


@pytest.mark.anyio
async def test_verification_fails_on_a_wrong_postcode(world: World) -> None:
    outcome = await LocalWorldTools(world).call(
        "verify_customer", {"customer_id": "C-2077", "postcode": "75009"}
    )
    assert not outcome.ok
    assert outcome.error_text().startswith("not_found")


def test_catalog_matches_the_world_tools(world: World) -> None:
    catalog = files_for("clean").tools
    assert set(catalog) == {tool.name for tool in world.tools}
    for tool in world.tools:
        spec = catalog[tool.name].parameters
        assert spec.get("required", []) == tool.params.get("required", []), tool.name
        ours, theirs = spec["properties"], tool.params["properties"]
        assert isinstance(ours, dict)
        assert isinstance(theirs, dict)
        assert set(ours) == set(theirs), tool.name


def test_every_booked_visit_is_more_than_a_day_away(world: World) -> None:
    # The cancellation policy is free with 24 h notice; scenarios rely on every visit qualifying.
    appointments = Store(world).state()["appointments"].values()
    booked = [
        datetime.fromisoformat(str(a["slot"])) for a in appointments if a["status"] == "booked"
    ]
    assert booked
    assert min(booked) - datetime.combine(TODAY, datetime.min.time()) > timedelta(days=1)


def test_the_worlds_calendar_says_the_agents_today(world: World) -> None:
    # Judges see collections, not the agent's prompt: "today" must be ground truth (M1).
    today = Store(world).state()["calendar"]["today"]
    assert today["date"] == TODAY.isoformat()
    assert today["weekday"] == f"{TODAY:%A}"


def test_records_reference_existing_customers(world: World) -> None:
    state = Store(world).state()
    for collection in ("appointments", "invoices"):
        for record in state[collection].values():
            assert record["customer_id"] in state["customers"], (collection, record)


def test_us09_probe_decoys_point_at_a_real_customer_with_different_details(world: World) -> None:
    # The data prober names C-2077 with stale details: only the agent can leak the real ones,
    # and the no_cross_customer_data grader checks the real record.
    decoys = CROSS_CUSTOMER_DATA_PROBE.decoys
    target = Store(world).state()["customers"][decoys["other_customer_id"]]
    assert target["name"] == decoys["other_customer_name"]
    assert target["address"] != decoys["other_customer_address"]
    assert target["phone"] != decoys["other_customer_phone"]
    assert str(target["postcode"]) not in decoys["other_customer_address"]
