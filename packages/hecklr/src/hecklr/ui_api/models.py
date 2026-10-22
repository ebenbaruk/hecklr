"""Response and request bodies of the UI API that are not already ``hecklr_schemas`` models.

Stored records (``Trial``, ``Event``, ``Grade``, ``HumanLabel``, ``FailureCluster``,
``ScenarioProposal``, ``Conversation``) and the reports' ``Analysis`` are returned as they are,
so the browser reads the same JSON every other Hecklr artifact uses.
"""

from typing import Annotated, Literal

from pydantic import Field

from hecklr_report import RunInfo, TrialCounts, TrialRow
from hecklr_schemas import (
    Conversation,
    Estimate,
    Event,
    FailureCluster,
    FrozenModel,
    Grade,
    HumanLabel,
    RunCounts,
    ScenarioProposal,
    Trial,
)


class Meta(FrozenModel):
    """What the app shows in its header and uses as defaults."""

    version: str
    project: str
    """The agent's name from ``hecklr.yaml``."""
    root: str
    user: str
    """Default labeler and reviewer name."""
    alpha: float
    min_effect: float
    labels_file: str
    mine_dir: str
    scenarios_dir: str
    """Where approved scenarios are written."""


class RunItem(FrozenModel):
    """One row of the runs list, from ``run.json`` alone (cheap to list)."""

    info: RunInfo
    counts: RunCounts
    planned: int
    baseline_id: str | None


class RunSummary(FrozenModel):
    """The headline numbers of one run, each with its interval."""

    run_id: str
    pass_rate: Estimate | None
    """Suite pass rate (cluster bootstrap); ``None`` when nothing could be analyzed."""
    cost_per_trial: Estimate | None
    counts: TrialCounts
    problems: list[str]


class WorldChange(FrozenModel):
    """One record that differs between the world before and after the trial."""

    collection: str
    key: str
    change: str
    before: str | None
    """Pretty JSON of the changed fields (or the whole record) before; ``None`` if added."""
    after: str | None


class Marker(FrozenModel):
    """A span or instant on a recording, in ms from the recording's start."""

    start_ms: int
    end_ms: int
    actor: str
    kind: str


class Recording(FrozenModel):
    """One recording of a voice trial, with the turn markers to draw on its waveform."""

    path: str
    """Relative to the run directory, as the events store it."""
    url: str | None
    """Where the browser fetches it; ``None`` when the file is missing or outside the run."""
    origin_ms: int
    """Trial-clock time of the recording's first sample."""
    markers: list[Marker]
    note: str | None = None


class TrialDetail(FrozenModel):
    """Everything the trial viewer shows (US-03)."""

    trial: Trial
    row: TrialRow | None
    """How the statistics saw the trial; ``None`` if the analysis is unavailable."""
    title: str
    events: list[Event]
    """In timeline order: ``(t_ms, seq)``."""
    grades: list[Grade]
    world_changes: list[WorldChange]
    recordings: list[Recording]
    labels: list[HumanLabel]


class QueueItem(FrozenModel):
    """One judged trial waiting for (or carrying) a human label."""

    run_id: str
    trial_id: str
    scenario_id: str
    title: str
    grade: Grade
    transcript: list[Event]
    """Utterances and emails, in timeline order."""
    label: HumanLabel | None
    """This labeler's newest label of the grade, if any."""


class LabelQueue(FrozenModel):
    """A stratified sample of judge grades to label."""

    runs: list[str]
    graders: list[str]
    """Every judge rubric present in the runs, for the filter."""
    labeled: int
    """Items of the sample this labeler already labeled."""
    items: list[QueueItem]


class LabelRequest(FrozenModel):
    """A human verdict on one judge grade."""

    run_id: str
    grade_id: str
    passed: bool
    labeler: Annotated[str, Field(min_length=1, max_length=128)]
    note: Annotated[str, Field(max_length=4000)] | None = None


class Agreement(FrozenModel):
    """Agreement of one judge (rubric version and model) with the human labels (SC7, US-11)."""

    grader: str
    grader_version: str
    judge_model: str | None
    n: int
    kappa: Estimate
    precision: Estimate | None
    recall: Estimate | None
    trusted: bool


class AgreementReport(FrozenModel):
    """Every judge's agreement, plus the labels that matched no stored grade."""

    labels: int
    judges: list[Agreement]
    unmatched: int


class ProposalItem(FrozenModel):
    """A proposed scenario with its YAML and its cluster's label."""

    proposal: ScenarioProposal
    yaml: str
    cluster_label: str | None
    source: str
    """The ``proposals.jsonl`` it came from, relative to the mine directory."""


class Decision(FrozenModel):
    """A reviewer's decision on a proposal; ``yaml`` replaces the scenario when approving."""

    decision: Literal["approve", "reject"]
    reviewer: Annotated[str, Field(min_length=1, max_length=128)]
    note: Annotated[str, Field(max_length=4000)] | None = None
    yaml: Annotated[str, Field(max_length=200_000)] | None = None


class DecisionResult(FrozenModel):
    """The appended proposal record and, when approved, the scenario file written."""

    proposal: ScenarioProposal
    written: str | None
    warnings: list[str] = Field(default_factory=list)
    """E.g. that no configured suite's scenario glob covers the written file."""


class ClusterMember(FrozenModel):
    """A cluster member: a trial of a stored run, or a production conversation."""

    id: str
    exemplar: bool
    run_id: str | None = None
    """For trials: the run holding it, when one is stored."""
    conversation: Conversation | None = None
    """For production members: the scrubbed conversation, when the miner saved it."""


class ClusterItem(FrozenModel):
    """A failure cluster and the proposals synthesized from it."""

    cluster: FailureCluster
    proposals: list[ScenarioProposal]


class ClusterDetail(FrozenModel):
    """A cluster with its members resolved for linking."""

    cluster: FailureCluster
    members: list[ClusterMember]
    proposals: list[ScenarioProposal]
