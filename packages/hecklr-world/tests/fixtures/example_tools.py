"""Python tools of the example world (``PythonOp`` entry points)."""

from datetime import UTC, datetime

from pydantic import JsonValue

from hecklr_world import NotFoundError, Store, ToolError, ToolTimeoutError


class NotAnEmergencyError(ToolError):
    code = "tool_failed"


def escalate_emergency(world: Store, customer_id: str, reason: str) -> JsonValue:
    """Create a handoff record for the on-call human after checking the customer exists."""
    world.get("customers", customer_id)
    if not reason.strip():
        raise NotAnEmergencyError("an emergency needs a reason")
    return world.create("handoffs", {"customer_id": customer_id, "reason": reason, "urgent": True})


def crash(world: Store) -> JsonValue:
    """A tool with a bug, to test how crashes surface."""
    raise ZeroDivisionError("division by zero")


def missing_record(world: Store) -> JsonValue:
    """A tool that reports a not-found error of its own."""
    raise NotFoundError("nothing here")


def upstream_timeout(world: Store) -> JsonValue:
    """A tool whose own upstream timed out."""
    raise ToolTimeoutError("upstream timed out")


def returns_a_datetime(world: Store) -> JsonValue:
    """A tool with a bug: it returns a value that is not JSON."""
    return {"when": datetime(2026, 10, 23, 9, tzinfo=UTC)}  # type: ignore[dict-item]  # the bug under test


not_callable = 42
