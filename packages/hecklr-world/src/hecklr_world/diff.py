"""World-state diffs for reports: what the trial added, removed and changed."""

import re
from typing import Literal

from pydantic import JsonValue

from hecklr_schemas.common import FrozenModel
from hecklr_schemas.world import WorldSnapshot

_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


class WorldChange(FrozenModel):
    """One difference between two snapshots.

    ``path`` uses the state-expression syntax (``appointments['C-1042'].slot``), so a report line
    can be pasted into a ``state`` criterion.
    """

    path: str
    kind: Literal["added", "removed", "changed"]
    before: JsonValue = None
    after: JsonValue = None


def _field(path: str, name: str) -> str:
    return f"{path}.{name}" if _IDENTIFIER.match(name) else f"{path}[{name!r}]"


def _diff_values(path: str, before: JsonValue, after: JsonValue) -> list[WorldChange]:
    if isinstance(before, dict) and isinstance(after, dict):
        changes: list[WorldChange] = []
        for name in sorted(before.keys() | after.keys()):
            sub = _field(path, name)
            if name not in after:
                changes.append(WorldChange(path=sub, kind="removed", before=before[name]))
            elif name not in before:
                changes.append(WorldChange(path=sub, kind="added", after=after[name]))
            else:
                changes.extend(_diff_values(sub, before[name], after[name]))
        return changes
    if before == after:
        return []
    return [WorldChange(path=path, kind="changed", before=before, after=after)]


def diff_snapshots(before: WorldSnapshot, after: WorldSnapshot) -> list[WorldChange]:
    """Every added, removed or changed record and field, sorted by collection, key and field.

    A whole record added or removed is one change at the record's path; a record present in both
    yields one change per differing field (recursing into nested objects; lists compare whole).
    """
    changes: list[WorldChange] = []
    for collection in sorted(before.collections.keys() | after.collections.keys()):
        old = before.collections.get(collection, {})
        new = after.collections.get(collection, {})
        for key in sorted(old.keys() | new.keys()):
            path = f"{collection}[{key!r}]"
            if key not in new:
                changes.append(WorldChange(path=path, kind="removed", before=old[key]))
            elif key not in old:
                changes.append(WorldChange(path=path, kind="added", after=new[key]))
            else:
                changes.extend(_diff_values(path, old[key], new[key]))
    return changes
