"""Errors of the world sandbox.

Two families, kept apart on purpose:

- ``ToolError``: the agent did something the backend refuses (unknown record, bad arguments). It
  is returned to the agent as a structured error, exactly as a real backend would answer, and is
  part of the conversation being tested.
- ``WorldConfigError``: the world or scenario is wrong (bad seed data, unknown entry point, a fault
  on a missing tool). It is a harness failure and is raised loudly when the trial's world opens.
"""

from typing import ClassVar, Literal

from pydantic import JsonValue

ToolErrorCode = Literal[
    "invalid_arguments",
    "unknown_tool",
    "not_found",
    "conflict",
    "tool_failed",
    "internal_error",
    "timeout",
]


class ToolError(Exception):
    """A tool call the backend refuses; Python tools raise it (or a subclass) to fail a call."""

    code: ClassVar[ToolErrorCode] = "tool_failed"
    http_status: ClassVar[int] = 400

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message

    def to_json(self) -> dict[str, JsonValue]:
        """The error body the agent receives over HTTP and MCP."""
        return {"error": {"code": self.code, "message": self.message}}


class InvalidArgumentsError(ToolError):
    """The arguments do not satisfy the tool's JSON Schema (or the record schema)."""

    code = "invalid_arguments"
    http_status = 400


class UnknownToolError(ToolError):
    """The world has no tool with this name."""

    code = "unknown_tool"
    http_status = 404


class NotFoundError(ToolError):
    """No record matches."""

    code = "not_found"
    http_status = 404


class ConflictError(ToolError):
    """A record with this key already exists."""

    code = "conflict"
    http_status = 409


class InternalToolError(ToolError):
    """The backend failed: an injected ``http_500`` or a Python tool that crashed."""

    code = "internal_error"
    http_status = 500


class ToolTimeoutError(ToolError):
    """An injected ``timeout`` call was released because the trial ended."""

    code = "timeout"
    http_status = 504


class WorldConfigError(ValueError):
    """The world definition or the scenario's faults cannot be served."""
