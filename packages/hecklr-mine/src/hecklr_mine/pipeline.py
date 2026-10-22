"""The miner end to end: scrub, detect, cluster, synthesize (spec → "Miner", US-08).

``mine`` takes conversations already ingested (stage 1, ``load_logs``) and returns everything the
later stages produced; ``hecklr_mine.store`` writes it to disk.
"""

from collections.abc import Sequence
from dataclasses import dataclass, field
from decimal import Decimal

import anyio
import structlog

from hecklr_llm import LLMOutputError
from hecklr_mine.cluster import (
    ClusterLabel,
    Group,
    group_failures,
    keyword_label,
    llm_label,
    make_cluster,
)
from hecklr_mine.detect import FailureReport, detect_failures, llm_summary
from hecklr_mine.embed import Embedder
from hecklr_mine.errors import MineError
from hecklr_mine.scrub import LlmPiiFinder, Scrubber, scrub_conversations
from hecklr_mine.synthesize import Duplicate, deduplicate, propose, synthesize_scenario
from hecklr_schemas import (
    Conversation,
    FailureCluster,
    LLMProvider,
    LLMRequest,
    LLMResponse,
    Scenario,
    ScenarioProposal,
)
from hecklr_sim import ReplayExtractionError

log = structlog.get_logger(__name__)


class BudgetExceededError(MineError):
    """The mining run reached its spending cap."""


@dataclass(frozen=True, slots=True)
class MineSettings:
    """What a mining run does, beside its inputs."""

    world: str
    """World id of the synthesized scenarios."""
    persona: str
    """Persona id the synthesized scenarios run with."""
    model: str | None = None
    """LLM for cluster labels, scenario synthesis and the optional LLM scrub and summaries;
    without one the miner stops after clustering, with keyword labels."""
    judge_model: str | None = None
    rubrics: tuple[str, ...] = ()
    """``hecklr_grade`` rubrics judged on every conversation (e.g. ``resolved``)."""
    llm_scrub: bool = False
    llm_summaries: bool = False
    """Embed an LLM-written root-cause sentence per failure instead of the transcript text."""
    min_cluster_size: int | None = None
    """``None``: 2.5% of the failures, at least 5."""
    exemplars: int = 3
    dedup_threshold: float = 0.9
    """Cosine similarity at or above which a proposal duplicates an existing scenario."""
    max_usd: Decimal | None = None
    """Stop (``BudgetExceededError``) once model and embedding spend reach this."""
    seed: int = 0
    concurrency: int = 8

    def __post_init__(self) -> None:
        """Reject settings that need a model without one."""
        if (self.llm_scrub or self.llm_summaries) and self.model is None:
            raise ValueError("llm_scrub and llm_summaries need a model")
        if self.rubrics and self.judge_model is None:
            raise ValueError("judge rubrics need a judge_model")


@dataclass
class MineResult:
    """Everything one mining run produced."""

    conversations: list[Conversation]
    """Scrubbed, in input order."""
    reports: list[FailureReport]
    """One per conversation, in input order; failures carry their ``cluster_id``."""
    clusters: list[FailureCluster]
    proposals: list[ScenarioProposal]
    duplicates: list[Duplicate] = field(default_factory=list)
    synthesis_errors: dict[str, str] = field(default_factory=dict)
    """Cluster id -> why no scenario could be synthesized for it."""
    placeholders: dict[str, int] = field(default_factory=dict)
    """Distinct scrubbed entities by kind."""
    embedder: str = ""
    llm_usd: Decimal = Decimal(0)
    embedding_usd: Decimal = Decimal(0)

    @property
    def failures(self) -> list[FailureReport]:
        """Reports of failed conversations."""
        return [report for report in self.reports if report.failed]


class _Meter:
    """An ``LLMProvider`` that adds up spend and refuses calls past the cap."""

    def __init__(self, inner: LLMProvider, cap: Decimal | None, embedder: Embedder) -> None:
        self._inner = inner
        self._cap = cap
        self._embedder = embedder
        self.spent = Decimal(0)

    def check(self) -> None:
        total = self.spent + self._embedder.spent_usd
        if self._cap is not None and total >= self._cap:
            raise BudgetExceededError(f"mining stopped: spend ${total:.4f} reached the cap")

    async def complete(self, request: LLMRequest) -> LLMResponse:
        self.check()
        response = await self._inner.complete(request)
        self.spent += response.cost_usd
        return response


def default_min_cluster_size(failures: int) -> int:
    """2.5% of the failures, at least 5: small dense groups of one phrasing are not causes."""
    return max(5, round(0.025 * failures))


async def mine(
    conversations: Sequence[Conversation],
    settings: MineSettings,
    *,
    embedder: Embedder,
    llm: LLMProvider | None = None,
    scrubber: Scrubber | None = None,
    existing: Sequence[Scenario] = (),
) -> MineResult:
    """Run stages 2 to 5 on ingested conversations.

    ``existing`` holds the suite's scenarios and queued proposals, for deduplication.

    Raises:
        ValueError: ``settings`` need a model but ``llm`` is ``None``.
        BudgetExceededError: Spend reached ``settings.max_usd``.
    """
    if llm is None and (settings.model is not None or settings.judge_model is not None):
        raise ValueError("settings name a model but no LLM provider was given")
    scrubber = scrubber or Scrubber()
    meter = _Meter(llm, settings.max_usd, embedder) if llm is not None else None
    model = settings.model if meter is not None else None

    finder = LlmPiiFinder(meter, model, seed=settings.seed) if meter and model else None
    scrubbed = await scrub_conversations(
        conversations,
        scrubber,
        llm_finder=finder if settings.llm_scrub else None,
        concurrency=settings.concurrency,
    )
    log.info("mine.scrubbed", conversations=len(scrubbed), entities=scrubber.placeholders.counts)
    reports = await detect_failures(
        scrubbed,
        llm=meter,
        judge_model=settings.judge_model,
        rubrics=settings.rubrics,
        seed=settings.seed,
        concurrency=settings.concurrency,
    )
    by_id = {conversation.id: conversation for conversation in scrubbed}
    failures = [report for report in reports if report.failed]
    if settings.llm_summaries and meter is not None and model is not None:
        failures = await _summarize(failures, by_id, meter, model, settings)
    log.info("mine.detected", failures=len(failures))

    clusters, failures = await _cluster(failures, embedder, meter, model, settings)
    if meter is not None:
        meter.check()
    log.info("mine.clustered", clusters=len(clusters))
    updated = {report.conversation_id: report for report in failures}
    result = MineResult(
        conversations=scrubbed,
        reports=[updated.get(report.conversation_id, report) for report in reports],
        clusters=clusters,
        proposals=[],
        placeholders=scrubber.placeholders.counts,
        embedder=embedder.name,
    )
    if meter is not None and model is not None:
        await _synthesize(result, by_id, meter, model, embedder, existing, settings)
    result.llm_usd = meter.spent if meter is not None else Decimal(0)
    result.embedding_usd = embedder.spent_usd
    return result


async def _summarize(
    failures: list[FailureReport],
    by_id: dict[str, Conversation],
    llm: LLMProvider,
    model: str,
    settings: MineSettings,
) -> list[FailureReport]:
    summaries: dict[int, str] = {}
    limiter = anyio.CapacityLimiter(settings.concurrency)

    async def summarize(index: int, report: FailureReport) -> None:
        async with limiter:
            conversation = by_id[report.conversation_id]
            summaries[index] = await llm_summary(
                conversation, report, llm, model, seed=settings.seed
            )

    async with anyio.create_task_group() as group:
        for index, report in enumerate(failures):
            group.start_soon(summarize, index, report)
    return [
        report.model_copy(update={"summary": summaries[index]})
        for index, report in enumerate(failures)
    ]


async def _cluster(
    failures: list[FailureReport],
    embedder: Embedder,
    llm: LLMProvider | None,
    model: str | None,
    settings: MineSettings,
) -> tuple[list[FailureCluster], list[FailureReport]]:
    if not failures:
        return [], failures
    summaries = [report.summary for report in failures]
    vectors = await embedder.embed(summaries)
    size = settings.min_cluster_size or default_min_cluster_size(len(failures))
    groups = group_failures(vectors, min_cluster_size=size)
    labels = await _labels(groups, summaries, llm, model, settings)
    clusters = []
    assigned: dict[int, str] = {}
    for group, label in zip(groups, labels, strict=True):
        ids = [failures[i].conversation_id for i in group.members]
        exemplars = [failures[i].conversation_id for i in group.exemplars(settings.exemplars)]
        cluster = make_cluster(label, ids, exemplars)
        clusters.append(cluster)
        assigned.update(dict.fromkeys(group.members, cluster.id))
    return clusters, [
        report.model_copy(update={"cluster_id": assigned.get(index)})
        for index, report in enumerate(failures)
    ]


async def _labels(
    groups: Sequence[Group],
    summaries: Sequence[str],
    llm: LLMProvider | None,
    model: str | None,
    settings: MineSettings,
) -> list[ClusterLabel]:
    labels = []
    for group in groups:
        members = [summaries[i] for i in group.members]
        if llm is None or model is None:
            labels.append(keyword_label(members, summaries))
            continue
        try:
            labels.append(await llm_label(members, llm, model, seed=settings.seed))
        except LLMOutputError as exc:
            log.warning("mine.label_failed", error=str(exc))
            labels.append(keyword_label(members, summaries))
    return labels


async def _synthesize(
    result: MineResult,
    by_id: dict[str, Conversation],
    llm: LLMProvider,
    model: str,
    embedder: Embedder,
    existing: Sequence[Scenario],
    settings: MineSettings,
) -> None:
    candidates: list[tuple[FailureCluster, Scenario]] = []
    for cluster in result.clusters:
        exemplar = by_id[cluster.exemplar_conversation_ids[0]]
        try:
            scenario = await synthesize_scenario(
                cluster,
                exemplar,
                llm,
                model=model,
                world=settings.world,
                persona=settings.persona,
                seed=settings.seed,
            )
        except (LLMOutputError, ReplayExtractionError) as exc:
            result.synthesis_errors[cluster.id] = str(exc)
            continue
        candidates.append((cluster, scenario))
    kept, result.duplicates = await deduplicate(
        candidates, existing, embedder, threshold=settings.dedup_threshold
    )
    result.proposals = [propose(cluster, scenario) for cluster, scenario in kept]
