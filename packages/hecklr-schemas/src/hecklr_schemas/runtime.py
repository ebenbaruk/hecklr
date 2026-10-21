"""Produced entities: runs, trials, grades, comparisons, failure clusters and patches.

Invariants every workstream relies on: IDs are ULIDs; times are integer milliseconds on the harness
monotonic clock (``datetime`` fields are wall-clock metadata only, always UTC-aware); a trial is
reproducible from (``config_hash``, ``seed``, cached model responses); artifacts are append-only.
"""

import math
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Literal, Self

from pydantic import AwareDatetime, Field, model_validator

from hecklr_schemas.common import (
    Budget,
    Channel,
    FrozenModel,
    Identifier,
    ModelName,
    NonNegativeMs,
    Probability,
    SchemaVersion,
)
from hecklr_schemas.events import EventRef
from hecklr_schemas.ids import (
    ClusterIdField,
    ComparisonIdField,
    GradeIdField,
    PatchIdField,
    RunIdField,
    TrialIdField,
)
from hecklr_schemas.world import WorldSnapshot

Usd = Annotated[Decimal, Field(ge=0)]
Count = Annotated[int, Field(ge=0)]


class TrialSpec(FrozenModel):
    """One cell of the run matrix: scenario x persona x channel x trial index."""

    scenario_id: Identifier
    persona_id: Identifier
    channel: Channel
    index: Count
    seed: int
    """Derived from the run seed and the other fields; drives every random choice in the trial."""

    @property
    def key(self) -> tuple[str, str, Channel, int]:
        """Identity of the cell, stable across runs; used to pair baseline and candidate."""
        return (self.scenario_id, self.persona_id, self.channel, self.index)


class AgentRef(FrozenModel):
    """Which agent, and which version of it, a run tested."""

    name: Identifier
    version: str | None = None
    """Free-form label, e.g. a release tag or prompt version."""
    adapters: dict[Channel, str] = Field(default_factory=dict)
    """Adapter type used per channel."""


class RunStatus(StrEnum):
    """Lifecycle of a run."""

    PENDING = "pending"
    RUNNING = "running"
    COMPLETED = "completed"
    BUDGET_EXCEEDED = "budget_exceeded"
    """Stopped cleanly when the cost budget ran out; finished trials are kept."""
    CANCELLED = "cancelled"
    FAILED = "failed"


class RunCounts(FrozenModel):
    """Trial outcomes so far; harness errors are excluded from statistics but always shown."""

    planned: Count = 0
    ok: Count = 0
    agent_error: Count = 0
    harness_error: Count = 0
    timeout: Count = 0

    @property
    def finished(self) -> int:
        """Trials that reached a terminal status."""
        return self.ok + self.agent_error + self.harness_error + self.timeout


class Run(FrozenModel):
    """One execution of a trial matrix against one agent version."""

    schema_version: SchemaVersion = 1
    id: RunIdField
    created_at: AwareDatetime
    git_sha: str | None = None
    agent: AgentRef
    config_hash: str
    """SHA-256 of the resolved configuration, scenarios, personas and world."""
    seed: int
    suite: Identifier | None = None
    matrix: list[TrialSpec]
    budget: Budget
    status: RunStatus = RunStatus.PENDING
    counts: RunCounts = RunCounts()
    spent_usd: Usd = Decimal(0)
    simulator_model: ModelName | None = None
    judge_model: ModelName | None = None


TrialStatus = Literal["ok", "agent_error", "harness_error", "timeout"]


class EndReason(StrEnum):
    """Why the conversation stopped, as reported by the simulator or the harness."""

    GOAL_ACHIEVED = "goal_achieved"
    GOAL_ABANDONED = "goal_abandoned"
    """The simulated customer ran out of patience."""
    HANDOFF = "handoff"
    MAX_TURNS = "max_turns"
    MAX_DURATION = "max_duration"
    AGENT_ENDED = "agent_ended"
    ERROR = "error"


class Trial(FrozenModel):
    """One conversation."""

    schema_version: SchemaVersion = 1
    id: TrialIdField
    run_id: RunIdField
    scenario_id: Identifier
    persona_id: Identifier
    channel: Channel
    index: Count
    seed: int
    status: TrialStatus
    end_reason: EndReason | None = None
    started_at: AwareDatetime
    duration_ms: NonNegativeMs
    cost_usd: Usd
    """Simulator and judge spend attributable to this trial."""
    world_before: WorldSnapshot
    world_after: WorldSnapshot
    turns: Count = 0
    contradictions: Count = 0
    """Simulator turns the self-consistency guard had to regenerate."""
    attempts: Annotated[int, Field(ge=1)] = 1
    """Harness-error retries consumed (at most 3)."""
    error: str | None = None

    @property
    def spec(self) -> TrialSpec:
        """The matrix cell this trial ran."""
        return TrialSpec(
            scenario_id=self.scenario_id,
            persona_id=self.persona_id,
            channel=self.channel,
            index=self.index,
            seed=self.seed,
        )


class Grade(FrozenModel):
    """One grader's verdict on one trial, with evidence."""

    schema_version: SchemaVersion = 1
    id: GradeIdField
    trial_id: TrialIdField
    grader: str
    """Grader name, e.g. ``tool_called`` or ``judge:resolved``."""
    grader_version: str
    criterion_index: Count | None = None
    """Position of the scenario success criterion this grade answers; ``None`` if diagnostic."""
    status: Literal["ok", "skipped", "error"] = "ok"
    """``skipped``: not applicable (e.g. voice metric on chat). ``error``: the grader failed."""
    value: bool | float
    passed: bool
    rationale: str | None = None
    evidence: list[EventRef] = Field(default_factory=list)
    judge_model: ModelName | None = None
    cost_usd: Usd = Decimal(0)

    @model_validator(mode="after")
    def _failed_status_cannot_pass(self) -> Self:
        if self.status == "error" and self.passed:
            raise ValueError("a grade in error status cannot pass")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("grade value must be finite")
        return self


class Verdict(StrEnum):
    """Outcome of comparing a candidate run against a baseline."""

    IMPROVEMENT = "improvement"
    REGRESSION = "regression"
    NO_CHANGE = "no_change"
    INCONCLUSIVE = "inconclusive"


class Comparison(FrozenModel):
    """Baseline vs candidate statistics for one metric on one slice."""

    schema_version: SchemaVersion = 1
    id: ComparisonIdField
    baseline: RunIdField
    candidate: RunIdField
    metric: str
    """E.g. ``pass_rate``, ``pass^3``, ``response_latency_p95``."""
    slice: dict[str, str] = Field(default_factory=dict)
    """Empty for the whole suite, else e.g. ``{"tag": "scheduling"}`` or ``{"snr_db": "10"}``."""
    baseline_value: float
    candidate_value: float
    delta: float
    ci_low: float
    ci_high: float
    confidence: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.95
    p_value: Probability
    q_value: Probability
    """Benjamini-Hochberg adjusted across every slice of the run."""
    verdict: Verdict
    alpha: Annotated[float, Field(gt=0.0, lt=1.0)]
    min_effect: Annotated[float, Field(ge=0.0)]
    n_scenarios: Count
    n_trials_baseline: Count
    n_trials_candidate: Count
    trials_needed: Count | None = None
    """For inconclusive verdicts: extra trials per scenario likely to reach a decision."""
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _interval_is_ordered(self) -> Self:
        if self.ci_low > self.ci_high:
            raise ValueError(f"ci_low {self.ci_low} exceeds ci_high {self.ci_high}")
        if self.trials_needed is not None and self.verdict is not Verdict.INCONCLUSIVE:
            raise ValueError("trials_needed only applies to inconclusive verdicts")
        return self


class FailureCluster(FrozenModel):
    """Failed trials or production conversations that share a root cause."""

    schema_version: SchemaVersion = 1
    id: ClusterIdField
    label: str
    """Short LLM-written cause, e.g. "fails when the customer gives two addresses"."""
    description: str | None = None
    source: Literal["run", "production"]
    trial_ids: list[TrialIdField] = Field(default_factory=list)
    conversation_ids: list[str] = Field(default_factory=list)
    exemplar_ids: list[str] = Field(default_factory=list)
    """The most representative members, shown first and used to synthesize scenarios."""
    proposed_scenario_ids: list[Identifier] = Field(default_factory=list)
    created_at: AwareDatetime

    @property
    def size(self) -> int:
        """Number of members."""
        return len(self.trial_ids) + len(self.conversation_ids)


class PatchStatus(StrEnum):
    """Where a candidate patch is in the fixer's pipeline."""

    PROPOSED = "proposed"
    REJECTED_TARGETED = "rejected_targeted"
    """No significant improvement on the cluster's scenarios."""
    REJECTED_GUARD = "rejected_guard"
    """Significant regression somewhere in the full suite."""
    ACCEPTED = "accepted"
    """Won both passes; ready for a human to merge."""


class Patch(FrozenModel):
    """An auto-fix proposal and its validation results."""

    schema_version: SchemaVersion = 1
    id: PatchIdField
    cluster_id: ClusterIdField
    candidate_index: Count
    files: Annotated[list[str], Field(min_length=1)]
    """Edited files; always a subset of ``agent.editable``."""
    diff: str
    """Unified diff against the baseline files."""
    rationale: str
    status: PatchStatus = PatchStatus.PROPOSED
    targeted: list[Comparison] = Field(default_factory=list)
    guard: list[Comparison] = Field(default_factory=list)
    generator_model: ModelName | None = None
    cost_usd: Usd = Decimal(0)
    created_at: AwareDatetime
