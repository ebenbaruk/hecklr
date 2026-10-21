"""Hecklr world sandbox: stateful mock backend exposed over MCP and HTTP.

``Sandbox`` implements ``hecklr_schemas.WorldSandbox`` and ``TrialWorld`` implements
``WorldSession``; ``load_world`` reads a world file with its seed files.
"""

from hecklr_world.app import openapi_document
from hecklr_world.diff import WorldChange, diff_snapshots
from hecklr_world.errors import (
    ConflictError,
    InternalToolError,
    InvalidArgumentsError,
    NotFoundError,
    ToolError,
    ToolTimeoutError,
    UnknownToolError,
    WorldConfigError,
)
from hecklr_world.loader import load_world
from hecklr_world.sandbox import Sandbox, ToolMode
from hecklr_world.session import ObservedToolCall, SessionClosedError, ToolOutcome, TrialWorld
from hecklr_world.store import Store

__version__ = "0.1.0"

__all__ = [
    "ConflictError",
    "InternalToolError",
    "InvalidArgumentsError",
    "NotFoundError",
    "ObservedToolCall",
    "Sandbox",
    "SessionClosedError",
    "Store",
    "ToolError",
    "ToolMode",
    "ToolOutcome",
    "ToolTimeoutError",
    "TrialWorld",
    "UnknownToolError",
    "WorldChange",
    "WorldConfigError",
    "diff_snapshots",
    "load_world",
    "openapi_document",
]
