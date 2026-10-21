"""Python tools of the example world (``PythonOp`` entry points)."""

from pydantic import JsonValue

from hecklr_world import NotFoundError, Store, ToolError


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


not_callable = 42
