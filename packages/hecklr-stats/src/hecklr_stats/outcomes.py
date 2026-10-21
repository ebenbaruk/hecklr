"""Plain per-trial records the statistics read, and their per-scenario pass counts.

The orchestrator turns trials and grades into ``TrialOutcome`` values, so this package never
depends on storage. Harness errors and ``error`` grades are excluded before this point (spec,
"Grade"); agent errors and timeouts are failed trials, not missing ones.
"""

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field

import numpy as np
import numpy.typing as npt

from hecklr_schemas import Slice

TAG_DIMENSION = "tag"
"""Slice key for scenario tags: a trial belongs to one ``{"tag": t}`` slice per tag it carries."""

SCENARIO_DIMENSION = "scenario"
"""Slice key of per-scenario summaries, ``{"scenario": id}``."""

IntArray = npt.NDArray[np.int64]
FloatArray = npt.NDArray[np.float64]


@dataclass(frozen=True, slots=True, kw_only=True)
class TrialOutcome:
    """What the statistics need to know about one finished trial.

    Attributes:
        scenario_id: The pairing and clustering unit.
        passed: Every success criterion of the scenario passed.
        labels: Single-valued slice labels, e.g. ``{"channel": "voice", "persona": "camille",
            "snr_db": "10"}``. Each key/value pair is one slice.
        tags: Scenario tags; each is its own ``{"tag": t}`` slice.
        latencies_ms: Response latencies of the agent's turns in this trial.
    """

    scenario_id: str
    passed: bool
    labels: Mapping[str, str] = field(default_factory=dict)
    tags: frozenset[str] = frozenset()
    latencies_ms: tuple[int, ...] = ()

    def __post_init__(self) -> None:
        reserved = {TAG_DIMENSION, SCENARIO_DIMENSION} & self.labels.keys()
        if reserved:
            raise ValueError(f"label keys {sorted(reserved)} are reserved slice dimensions")
        if any(latency < 0 for latency in self.latencies_ms):
            raise ValueError("latencies must be non-negative milliseconds")

    def slices(self) -> list[Slice]:
        """Every slice this trial belongs to, excluding the whole-suite slice ``{}``."""
        by_label = [{key: value} for key, value in sorted(self.labels.items())]
        return by_label + [{TAG_DIMENSION: tag} for tag in sorted(self.tags)]

    def in_slice(self, slice_: Slice) -> bool:
        """Whether this trial belongs to ``slice_`` (every trial belongs to ``{}``)."""
        return all(
            tag in self.tags if key == TAG_DIMENSION else self.labels.get(key) == tag
            for key, tag in slice_.items()
        )


def slices_of(*runs: Iterable[TrialOutcome]) -> list[Slice]:
    """Every slice any trial of ``runs`` belongs to (excluding ``{}``), sorted by key."""
    keys = {
        tuple(sorted(slice_.items()))
        for outcomes in runs
        for outcome in outcomes
        for slice_ in outcome.slices()
    }
    return [dict(key) for key in sorted(keys)]


def slice_key(slice_: Slice) -> str:
    """Stable text form of a slice, e.g. ``channel=voice``; empty for the whole suite."""
    return "&".join(f"{key}={value}" for key, value in sorted(slice_.items()))


@dataclass(frozen=True, slots=True)
class ScenarioCounts:
    """Trials and passes per scenario, sorted by scenario id.

    Attributes:
        scenario_ids: Scenario ids in ascending order.
        trials: Trials per scenario, each at least 1.
        passes: Passing trials per scenario.
    """

    scenario_ids: tuple[str, ...]
    trials: IntArray
    passes: IntArray

    def __post_init__(self) -> None:
        if not (len(self.scenario_ids) == self.trials.shape[0] == self.passes.shape[0]):
            raise ValueError("scenario_ids, trials and passes must have the same length")
        if np.any(self.trials < 1) or np.any(self.passes < 0) or np.any(self.passes > self.trials):
            raise ValueError("each scenario needs trials >= 1 and 0 <= passes <= trials")

    @classmethod
    def from_outcomes(cls, outcomes: Iterable[TrialOutcome]) -> "ScenarioCounts":
        """Count trials and passes of each scenario in ``outcomes``."""
        tallies: dict[str, list[int]] = {}
        for outcome in outcomes:
            tally = tallies.setdefault(outcome.scenario_id, [0, 0])
            tally[0] += 1
            tally[1] += int(outcome.passed)
        ids = tuple(sorted(tallies))
        return cls(
            scenario_ids=ids,
            trials=np.array([tallies[i][0] for i in ids], dtype=np.int64),
            passes=np.array([tallies[i][1] for i in ids], dtype=np.int64),
        )

    @classmethod
    def from_arrays(cls, trials: npt.ArrayLike, passes: npt.ArrayLike) -> "ScenarioCounts":
        """Build counts for anonymous scenarios ``s0000``, ``s0001``, ... (simulations, tests)."""
        trials_array = np.asarray(trials, dtype=np.int64)
        return cls(
            scenario_ids=tuple(f"s{i:04d}" for i in range(trials_array.shape[0])),
            trials=trials_array,
            passes=np.asarray(passes, dtype=np.int64),
        )

    @property
    def n_scenarios(self) -> int:
        """Number of scenarios."""
        return len(self.scenario_ids)

    @property
    def rates(self) -> FloatArray:
        """Observed pass rate of each scenario."""
        return self.passes / self.trials

    def select(self, scenario_ids: Iterable[str]) -> "ScenarioCounts":
        """The counts of ``scenario_ids`` only (each must be present)."""
        position = {sid: i for i, sid in enumerate(self.scenario_ids)}
        keep = sorted(position[sid] for sid in scenario_ids)
        return ScenarioCounts(
            scenario_ids=tuple(self.scenario_ids[i] for i in keep),
            trials=self.trials[keep],
            passes=self.passes[keep],
        )
