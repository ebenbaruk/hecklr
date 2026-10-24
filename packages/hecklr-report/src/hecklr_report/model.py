"""The ``Analysis`` record: everything every report format shows, computed once by ``analyze``.

Renderers only format an ``Analysis`` (plus the stored run for the per-trial HTML viewer); none of
them computes a statistic. It serializes to JSON as the machine-readable report, so it carries
``schema_version`` like every other Hecklr artifact.
"""

from decimal import Decimal
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field

from hecklr_schemas import (
    AgentRef,
    Channel,
    Comparison,
    Estimate,
    FrozenModel,
    MetricSummary,
    Run,
    RunStatus,
    Slice,
    StatsConfig,
    TrialStatus,
    Verdict,
)
from hecklr_schemas.common import ModelName, SchemaVersion
from hecklr_schemas.ids import RunIdField, TrialIdField
from hecklr_stats import PASS_RATE

Count = Annotated[int, Field(ge=0)]
FlipDirection = Literal["pass_to_fail", "fail_to_pass"]
ExclusionReason = Literal["harness_error", "grade_error"]


class RunInfo(FrozenModel):
    """Who and what a run tested, for report headers."""

    id: RunIdField
    suite: str | None
    agent: AgentRef
    git_sha: str | None
    created_at: AwareDatetime
    status: RunStatus
    seed: int
    adaptive: bool
    spent_usd: Decimal
    """Total model spend: an accounting fact, not an estimate."""
    simulator_model: ModelName | None
    judge_model: ModelName | None

    @classmethod
    def of(cls, run: Run) -> "RunInfo":
        """The header fields of ``run``."""
        return cls(
            id=run.id,
            suite=run.suite,
            agent=run.agent,
            git_sha=run.git_sha,
            created_at=run.created_at,
            status=run.status,
            seed=run.seed,
            adaptive=run.adaptive,
            spent_usd=run.spent_usd,
            simulator_model=run.simulator_model,
            judge_model=run.judge_model,
        )


class TrialCounts(FrozenModel):
    """How many trials a run has and which of them the statistics could use."""

    total: Count
    ok: Count
    agent_error: Count
    timeout: Count
    harness_error: Count
    """Excluded from statistics (spec, "Orchestrator"), always shown."""
    undecided: Count
    """Excluded because an ``error`` grade left the trial without a verdict."""
    grade_errors: Count
    """``error`` grades, on any trial."""
    analyzed: Count
    """Trials behind every pass-rate statistic: total minus harness errors minus undecided."""
    contradiction_rate: Estimate | None
    """Share of conversations where the simulator contradicted its brief at least once,
    counting those the consistency guard gave up on (ADR 0024-hstats)."""
    errors_after_contradiction: Count = 0
    """Harness errors in trials whose simulator contradicted its brief: mostly the consistency
    guard giving up on a turn, though a later failure of another kind is counted too."""


class FailureCause(FrozenModel):
    """Why one trial failed: the failing success criterion (or the trial status)."""

    cause: str
    """Grouping key, e.g. ``judge:resolved``, ``tool_called reschedule_appointment``,
    ``timeout``."""
    rationale: str | None = None
    """The grader's or judge's explanation, shortened."""


class TrialRow(FrozenModel):
    """One trial as the statistics saw it."""

    trial_id: TrialIdField
    scenario_id: str
    persona_id: str
    channel: Channel
    index: Count
    status: TrialStatus
    passed: bool | None
    """``None`` when the trial is excluded from statistics (see ``excluded``)."""
    excluded: ExclusionReason | None
    causes: list[FailureCause] = Field(default_factory=list)
    labels: dict[str, str] = Field(default_factory=dict)
    """Slice labels: channel, persona and, for voice, SNR bucket and accent."""
    duration_ms: Count
    cost_usd: Decimal


class ScenarioResult(FrozenModel):
    """One scenario's pass rate in the candidate (and baseline) run."""

    scenario_id: str
    title: str
    tags: list[str] = Field(default_factory=list)
    trials: Count
    passes: Count
    pass_rate: Estimate | None
    """Wilson interval; ``None`` when no trial of the scenario could be analyzed."""
    baseline_pass_rate: Estimate | None = None
    flip: FlipDirection | None = None
    """Majority pass on one side and majority fail on the other (as ``hecklr_stats`` defines)."""
    cause: FailureCause | None = None
    """The most common failure cause on the failing side, for failed or flipped scenarios."""
    flaky: Estimate | None = None
    """Pass rate pooled over both runs when they are declared identical and it lies in 20-80%: the
    scenario is flaky (spec, "Validating the statistics"); ``None`` otherwise."""


class FlipGroup(FrozenModel):
    """Scenarios that flipped in the same direction for the same cause (US-05)."""

    direction: FlipDirection
    cause: str
    scenario_ids: list[str]
    rationale: str | None = None
    """One representative explanation from the failing side."""


class DegradationPoint(FrozenModel):
    """Task success in one SNR bucket or accent (US-07)."""

    label: str
    trials: Count
    pass_rate: Estimate
    baseline_pass_rate: Estimate | None = None


class VoiceSection(FrozenModel):
    """Voice-only results: degradation curves and turn-taking metrics by slice (US-06, US-07)."""

    degradation: list[DegradationPoint]
    """Success rate by SNR bucket, cleanest first."""
    by_accent: list[DegradationPoint]
    metrics: list[MetricSummary]
    """Voice metrics on voice trials only, for the whole voice set (slice ``{}``) and per
    ``snr_db`` and ``accent`` slice; metric names are in ``hecklr_report.voice``."""


class JudgeTrust(FrozenModel):
    """Agreement of one judge rubric with human labels; κ below 0.70 is untrusted (SC7)."""

    grader: str
    grader_version: str
    judge_model: str | None
    n: Count
    kappa: Estimate
    trusted: bool
    reason: str | None = None
    """Why the rubric is untrusted (too few labels of a class, or κ below 0.70)."""


class Analysis(FrozenModel):
    """A run, optionally compared with a baseline, ready to render in any format."""

    schema_version: SchemaVersion = 1
    created_at: AwareDatetime
    candidate: RunInfo
    baseline: RunInfo | None = None
    stats: StatsConfig
    seed: int
    """Bootstrap and permutation seed actually used."""
    counts: TrialCounts
    baseline_counts: TrialCounts | None = None
    summaries: list[MetricSummary] = Field(default_factory=list)
    """Candidate metrics, each with its interval."""
    baseline_summaries: list[MetricSummary] = Field(default_factory=list)
    comparisons: list[Comparison] = Field(default_factory=list)
    """Suite pass rate first, then pass-rate slices, then latency comparisons."""
    verdict: Verdict | None = None
    """The suite pass-rate verdict; ``None`` without a usable baseline."""
    scenarios: list[ScenarioResult] = Field(default_factory=list)
    identical_runs: bool = False
    """The caller declared baseline and candidate the same agent and code (``hecklr calibrate
    --aa``), so scenarios were checked for flakiness (``ScenarioResult.flaky``)."""
    flips: list[FlipGroup] = Field(default_factory=list)
    voice: VoiceSection | None = None
    judges: list[JudgeTrust] = Field(default_factory=list)
    trials: list[TrialRow] = Field(default_factory=list)
    problems: list[str] = Field(default_factory=list)
    """Reasons the run cannot be trusted to gate (failed run, nothing analyzable, no baseline
    overlap); any problem makes ``gate_exit_code`` return 3."""

    def summary(self, metric: str, slice_: Slice | None = None) -> Estimate | None:
        """The candidate's estimate of ``metric`` on ``slice_`` (default: whole suite)."""
        return _find(self.summaries, metric, slice_ or {})

    def baseline_summary(self, metric: str, slice_: Slice | None = None) -> Estimate | None:
        """The baseline's estimate of ``metric`` on ``slice_`` (default: whole suite)."""
        return _find(self.baseline_summaries, metric, slice_ or {})

    @property
    def judge_model_change(self) -> tuple[str, str] | None:
        """``(baseline, candidate)`` judge models when both were recorded and they differ.

        The spec's judge drift check: verdicts of judge rubrics may then move with the judge,
        so reports warn and point to re-scoring the labeled set (issue #102).
        """
        if self.baseline is None:
            return None
        before, after = self.baseline.judge_model, self.candidate.judge_model
        if before is None or after is None or before == after:
            return None
        return before, after

    @property
    def suite_comparison(self) -> Comparison | None:
        """The whole-suite pass-rate comparison that decides the gate (ADR 0005b)."""
        return next(
            (c for c in self.comparisons if c.metric == PASS_RATE and not c.slice),
            None,
        )


def _find(summaries: list[MetricSummary], metric: str, slice_: Slice) -> Estimate | None:
    return next(
        (s.estimate for s in summaries if s.metric == metric and s.slice == slice_),
        None,
    )
