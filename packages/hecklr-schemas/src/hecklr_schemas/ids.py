"""ULID-based identifiers for produced entities.

ULIDs sort by creation time, so artifact directories and DuckDB rows list in the order they were
written without an extra timestamp column.
"""

from typing import Annotated, NewType

from pydantic import StringConstraints
from ulid import ULID

ULID_PATTERN = r"^[0-9A-HJKMNP-TV-Z]{26}$"
"""Crockford base32, 26 characters, upper case: the canonical ULID text form."""

_UlidStr = StringConstraints(pattern=ULID_PATTERN)

RunId = NewType("RunId", str)
TrialId = NewType("TrialId", str)
EventId = NewType("EventId", str)
GradeId = NewType("GradeId", str)
ComparisonId = NewType("ComparisonId", str)
ClusterId = NewType("ClusterId", str)
PatchId = NewType("PatchId", str)

# Field types that validate the ULID text form on load.
RunIdField = Annotated[RunId, _UlidStr]
TrialIdField = Annotated[TrialId, _UlidStr]
EventIdField = Annotated[EventId, _UlidStr]
GradeIdField = Annotated[GradeId, _UlidStr]
ComparisonIdField = Annotated[ComparisonId, _UlidStr]
ClusterIdField = Annotated[ClusterId, _UlidStr]
PatchIdField = Annotated[PatchId, _UlidStr]


def new_id() -> str:
    """Return a fresh ULID string; wrap it in the matching NewType, e.g. ``RunId(new_id())``."""
    return str(ULID())


def is_ulid(value: str) -> bool:
    """Return whether ``value`` is a canonical ULID string."""
    try:
        return str(ULID.from_str(value)) == value
    except ValueError:
        return False
