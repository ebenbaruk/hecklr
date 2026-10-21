"""Hecklr world sandbox: stateful mock backend exposed over MCP and HTTP.

``TrialWorld`` implements ``WorldSession``; ``load_world`` reads a world file with its seed files.
"""

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
from hecklr_world.session import ObservedToolCall, SessionClosedError, ToolOutcome, TrialWorld
from hecklr_world.store import Store

__version__ = "0.1.0"

__all__ = [
    "ConflictError",
    "InternalToolError",
    "InvalidArgumentsError",
    "NotFoundError",
    "ObservedToolCall",
    "SessionClosedError",
    "Store",
    "ToolError",
    "ToolOutcome",
    "ToolTimeoutError",
    "TrialWorld",
    "UnknownToolError",
    "WorldChange",
    "WorldConfigError",
    "diff_snapshots",
    "load_world",
]
