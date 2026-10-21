"""Produced entities: runs, trials, grades, statistics, clusters, proposals, labels and patches.

Invariants every workstream relies on: IDs are ULIDs; times are integer milliseconds on the harness
monotonic clock (``datetime`` fields are wall-clock metadata only, always UTC-aware); a trial is
reproducible from (``config_hash``, ``seed``, cached model responses); artifacts are append-only.
Records whose status changes over time (``Run``, ``ScenarioProposal``) are re-appended with the
same ``id`` and the newest record wins.
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
    LabelIdField,
    PatchIdField,
    ProposalIdField,
    RunIdField,
    TrialIdField,
)
from hecklr_schemas.scenario import Scenario
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
    baseline_id: RunIdField | None = None
    """The run this one is compared against, when there is one."""
    adaptive: bool = False
    """Extra trials were allocated after the first pass (``--adaptive``)."""
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


class JudgeVote(FrozenModel):
    """One independent judgment inside a multi-judge (majority vote) grade."""

    passed: bool
    rationale: str | None = None
    evidence: list[EventRef] = Field(default_factory=list)
    model: ModelName | None = None


class Grade(FrozenModel):
    """One grader's verdict on one trial, with evidence.

    Only ``ok`` grades decide a trial: "all success criteria pass" and every statistic read the
    ``ok`` grades and ignore ``skipped`` ones (e.g. a voice metric on a chat trial). ``error``
    grades are excluded from statistics like harness errors, and counted and shown the same way.
    """

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
    votes: list[JudgeVote] = Field(default_factory=list)
    """Individual judgments of a 3-judge majority; empty for single judges and other graders."""
    cost_usd: Usd = Decimal(0)

    @model_validator(mode="after")
    def _only_ok_grades_pass(self) -> Self:
        if self.status != "ok" and self.passed:
            raise ValueError(f"a grade in {self.status} status cannot pass")
        if isinstance(self.value, float) and not math.isfinite(self.value):
            raise ValueError("grade value must be finite")
        return self


class Verdict(StrEnum):
    """Outcome of comparing a candidate run against a baseline."""

    IMPROVEMENT = "improvement"
    REGRESSION = "regression"
    NO_CHANGE = "no_change"
    INCONCLUSIVE = "inconclusive"


class IntervalMethod(StrEnum):
    """How a confidence interval was computed (spec, "Intervals")."""

    WILSON = "wilson"
    """Wilson score interval: per-scenario proportions."""
    CLUSTER_BOOTSTRAP = "cluster_bootstrap"
    """Resample scenarios, then trials within each: suite-level metrics."""
    PAIRED_CLUSTER_BOOTSTRAP = "paired_cluster_bootstrap"
    """Cluster bootstrap of per-scenario differences: baseline vs candidate deltas."""
    BOOTSTRAP = "bootstrap"
    """Plain bootstrap, e.g. on a latency quantile."""


class Estimate(FrozenModel):
    """A number with its confidence interval: Hecklr never reports a bare metric."""

    value: float
    ci_low: float
    ci_high: float
    confidence: Annotated[float, Field(gt=0.0, lt=1.0)] = 0.95
    n: Count
    """Independent units behind the estimate: scenarios for cluster methods, else observations."""
    method: IntervalMethod

    @model_validator(mode="after")
    def _is_finite_and_ordered(self) -> Self:
        if not all(math.isfinite(v) for v in (self.value, self.ci_low, self.ci_high)):
            raise ValueError("estimate and interval bounds must be finite")
        if self.ci_low > self.ci_high:
            raise ValueError(f"ci_low {self.ci_low} exceeds ci_high {self.ci_high}")
        return self


Slice = dict[str, str]
"""Empty for the whole suite, else e.g. ``{"tag": "scheduling"}`` or ``{"snr_db": "10"}``."""


class MetricSummary(FrozenModel):
    """One run's value of one metric on one slice, e.g. a scenario's pass rate or the suite p95."""

    schema_version: SchemaVersion = 1
    run_id: RunIdField
    metric: str
    """E.g. ``pass_rate``, ``pass^3``, ``pass@3``, ``response_latency_p95``, ``cost_usd``."""
    slice: Slice = Field(default_factory=dict)
    estimate: Estimate


class Comparison(FrozenModel):
    """Baseline vs candidate statistics for one metric on one slice."""

    schema_version: SchemaVersion = 1
    id: ComparisonIdField
    baseline: RunIdField
    candidate: RunIdField
    metric: str
    """E.g. ``pass_rate``, ``pass^3``, ``response_latency_p95``."""
    slice: Slice = Field(default_factory=dict)
    baseline_estimate: Estimate
    candidate_estimate: Estimate
    delta: Estimate
    """Candidate minus baseline; for pass rates, the mean of paired per-scenario differences."""
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
    look: Annotated[int, Field(ge=1)] = 1
    """Group-sequential look this comparison was made at; always 1 without ``--adaptive``."""
    alpha_spent: Probability | None = None
    """Cumulative alpha spent through ``look`` (O'Brien-Fleming); ``None`` for a fixed design."""
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _verdict_fields_agree(self) -> Self:
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
    """Members when ``source`` is ``run``."""
    conversation_ids: list[str] = Field(default_factory=list)
    """Members when ``source`` is ``production``: ``Conversation.id`` values."""
    exemplar_trial_ids: list[TrialIdField] = Field(default_factory=list)
    exemplar_conversation_ids: list[str] = Field(default_factory=list)
    """The most representative members, shown first and used to synthesize scenarios."""
    created_at: AwareDatetime

    @model_validator(mode="after")
    def _members_match_source(self) -> Self:
        if self.source == "run" and (self.conversation_ids or self.exemplar_conversation_ids):
            raise ValueError("a cluster of run trials cannot hold conversation ids")
        if self.source == "production" and (self.trial_ids or self.exemplar_trial_ids):
            raise ValueError("a cluster of production conversations cannot hold trial ids")
        if not set(self.exemplar_trial_ids) <= set(self.trial_ids) or not set(
            self.exemplar_conversation_ids
        ) <= set(self.conversation_ids):
            raise ValueError("exemplars must be members of the cluster")
        return self

    @property
    def size(self) -> int:
        """Number of members."""
        return len(self.trial_ids) + len(self.conversation_ids)


class ProposalStatus(StrEnum):
    """Human review state of a proposed scenario."""

    PROPOSED = "proposed"
    APPROVED = "approved"
    """Accepted into the suite."""
    REJECTED = "rejected"


class ScenarioProposal(FrozenModel):
    """A scenario synthesized from a failure cluster, waiting for (or past) human review.

    Reviewing appends a copy with the decision under the same ``id`` (the newest record wins).
    """

    schema_version: SchemaVersion = 1
    id: ProposalIdField
    cluster_id: ClusterIdField
    scenario: Scenario
    status: ProposalStatus = ProposalStatus.PROPOSED
    reviewer: str | None = None
    note: str | None = None
    created_at: AwareDatetime
    decided_at: AwareDatetime | None = None

    @model_validator(mode="after")
    def _decision_is_complete(self) -> Self:
        decided = self.status is not ProposalStatus.PROPOSED
        if {decided} != {self.reviewer is not None, self.decided_at is not None}:
            raise ValueError("reviewer and decided_at are set exactly when a proposal is decided")
        return self


class HumanLabel(FrozenModel):
    """A person's pass/fail verdict on one judged grade, for judge calibration (Cohen's kappa).

    ``grader_version`` pins the rubric version labeled, so agreement is never computed across
    rubric changes. Relabeling appends a new label; the newest per (grade, labeler) wins.
    """

    schema_version: SchemaVersion = 1
    id: LabelIdField
    trial_id: TrialIdField
    grade_id: GradeIdField
    grader: str
    """E.g. ``judge:resolved``."""
    grader_version: str
    passed: bool
    labeler: Annotated[str, Field(min_length=1)]
    note: str | None = None
    created_at: AwareDatetime


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
