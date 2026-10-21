"""Deterministic fault schedules for one trial (``Scenario.faults``, ADR 0004)."""

import hashlib
import random
from collections import Counter, defaultdict
from collections.abc import Iterable

from pydantic import JsonValue

from hecklr_schemas.world import ToolFault
from hecklr_world.errors import WorldConfigError


def _tool_rng(seed: int, tool: str) -> random.Random:
    # One stream per tool: whether a call to one tool is faulted never depends on how many calls
    # other tools received, so interleaving (which the agent controls) cannot shift the schedule.
    digest = hashlib.sha256(f"{seed}:{tool}".encode()).digest()
    return random.Random(int.from_bytes(digest[:8], "big"))  # noqa: S311 - simulation, not security


class FaultPlan:
    """Decides, call by call, which injected fault (if any) shapes a tool call."""

    def __init__(self, faults: Iterable[ToolFault], seed: int, tools: Iterable[str]) -> None:
        known = set(tools)
        self._faults: dict[str, list[ToolFault]] = defaultdict(list)
        for fault in faults:
            if fault.tool not in known:
                raise WorldConfigError(f"fault targets unknown tool {fault.tool!r}")
            self._faults[fault.tool].append(fault)
        self._rngs = {tool: _tool_rng(seed, tool) for tool in self._faults}
        self._calls: Counter[str] = Counter()

    def next_fault(self, tool: str) -> ToolFault | None:
        """The fault for the next call to ``tool``; the first triggered fault in list order wins.

        Every eligible fault draws from the tool's RNG on every call, even when an earlier one
        already fired, so the schedule depends only on the seed and the call count.
        """
        index = self._calls[tool]
        self._calls[tool] += 1
        chosen: ToolFault | None = None
        for fault in self._faults.get(tool, []):
            if index < fault.after_calls:
                continue
            fires = self._rngs[tool].random() < fault.probability
            if fires and chosen is None:
                chosen = fault
        return chosen


def apply_stale_patch(result: JsonValue, patch: dict[str, JsonValue]) -> JsonValue:
    """Override ``patch`` fields on the returned record, or on every record of a list."""
    if isinstance(result, dict):
        return {**result, **patch}
    if isinstance(result, list):
        return [apply_stale_patch(item, patch) for item in result]
    return result
