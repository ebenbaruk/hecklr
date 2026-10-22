"""Mining artifacts on disk, and the scenario-proposal review queue (ADR 0013a).

One directory per mining run, ``<output_dir>/mine/<mine_id>/``::

    summary.json          MineSummary: counts, models, spend, duplicates, errors
    conversations.jsonl   scrubbed Conversation per line (exemplars are shown from here)
    failures.jsonl        FailureReport per failed conversation, with its cluster_id
    clusters.jsonl        FailureCluster per line
    proposals.jsonl       ScenarioProposal per line, append-only: a review appends a decided copy
    scenarios/<id>.yaml   each proposed scenario as authored YAML, for reading and diffing

The queue is every run's ``proposals.jsonl``; for each proposal id the last record wins.
"""

import os
from collections.abc import Iterable, Sequence
from dataclasses import asdict
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Final

from pydantic import AwareDatetime, BaseModel, ConfigDict, Field

from hecklr_mine.errors import MineError
from hecklr_mine.pipeline import MineResult
from hecklr_schemas import ProposalStatus, Scenario, ScenarioProposal
from hecklr_sim import scenario_to_yaml

MINE_DIR: Final = "mine"
"""Mining runs live in ``<output_dir>/mine/<mine_id>/``."""
SUMMARY_FILE: Final = "summary.json"
CONVERSATIONS_FILE: Final = "conversations.jsonl"
FAILURES_FILE: Final = "failures.jsonl"
CLUSTERS_FILE: Final = "clusters.jsonl"
PROPOSALS_FILE: Final = "proposals.jsonl"
SCENARIOS_DIR: Final = "scenarios"


class MineSummary(BaseModel):
    """``summary.json``: what one mining run did, for the CLI, the UI and people."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    schema_version: int = 1
    mine_id: str
    created_at: AwareDatetime
    source: str
    """The log file mined."""
    log_format: str
    conversations: int
    failures: int
    signals: dict[str, int]
    """Failed conversations per signal kind."""
    clusters: int
    noise: int
    """Failures that joined no cluster."""
    proposals: int
    duplicates: list[dict[str, str | float]] = Field(default_factory=list)
    synthesis_errors: dict[str, str] = Field(default_factory=dict)
    judge_errors: int = 0
    scrub_errors: dict[str, str] = Field(default_factory=dict)
    """Conversation id -> why the LLM scrub pass failed on it; its pattern scrubbing stands."""
    summary_errors: dict[str, str] = Field(default_factory=dict)
    """Conversation id -> why its LLM summary failed; the heuristic summary was embedded."""
    placeholders: dict[str, int] = Field(default_factory=dict)
    embedder: str
    model: str | None = None
    llm_usd: Decimal
    embedding_usd: Decimal


def summarize(
    result: MineResult, *, mine_id: str, source: str, log_format: str, model: str | None
) -> MineSummary:
    """The summary record of ``result``."""
    failures = result.failures
    signals: dict[str, int] = {}
    for report in failures:
        for kind in dict.fromkeys(signal.kind.value for signal in report.signals):
            signals[kind] = signals.get(kind, 0) + 1
    return MineSummary(
        mine_id=mine_id,
        created_at=datetime.now(UTC),
        source=source,
        log_format=log_format,
        conversations=len(result.conversations),
        failures=len(failures),
        signals=dict(sorted(signals.items())),
        clusters=len(result.clusters),
        noise=sum(report.cluster_id is None for report in failures),
        proposals=len(result.proposals),
        duplicates=[asdict(duplicate) for duplicate in result.duplicates],
        synthesis_errors=result.synthesis_errors,
        judge_errors=sum(len(report.judge_errors) for report in result.reports),
        scrub_errors=result.scrub_errors,
        summary_errors=result.summary_errors,
        placeholders=result.placeholders,
        embedder=result.embedder,
        model=model,
        llm_usd=result.llm_usd,
        embedding_usd=result.embedding_usd,
    )


def write_run(directory: Path, result: MineResult, summary: MineSummary) -> None:
    """Write a mining run's artifacts into a new directory.

    Raises:
        FileExistsError: ``directory`` already holds a mining run (artifacts are never
            overwritten).
    """
    if (directory / SUMMARY_FILE).exists() or (directory / PROPOSALS_FILE).exists():
        raise FileExistsError(f"{directory} already holds a mining run")
    scenarios = directory / SCENARIOS_DIR
    scenarios.mkdir(parents=True, exist_ok=True)
    _write_lines(directory / CONVERSATIONS_FILE, result.conversations)
    _write_lines(directory / FAILURES_FILE, result.failures)
    _write_lines(directory / CLUSTERS_FILE, result.clusters)
    append_proposals(directory / PROPOSALS_FILE, result.proposals)
    for proposal in result.proposals:
        path = scenarios / f"{proposal.scenario.id}.yaml"
        path.write_text(scenario_to_yaml(proposal.scenario), encoding="utf-8")
    # Written last: its presence marks a complete run for readers such as the UI.
    (directory / SUMMARY_FILE).write_text(summary.model_dump_json(indent=2) + "\n", "utf-8")


def _write_lines(path: Path, records: Iterable[BaseModel]) -> None:
    path.write_text("".join(record.model_dump_json() + "\n" for record in records), "utf-8")


def append_proposals(path: Path, proposals: Sequence[ScenarioProposal]) -> None:
    """Append proposal records, each as one complete line in one write.

    One ``write`` per record on a file opened for appending keeps concurrent writers (the CLI
    and the UI) from interleaving parts of lines.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(path, os.O_WRONLY | os.O_APPEND | os.O_CREAT, 0o644)
    try:
        for proposal in proposals:
            os.write(descriptor, (proposal.model_dump_json() + "\n").encode())
    finally:
        os.close(descriptor)


def read_proposals(path: Path) -> list[ScenarioProposal]:
    """Every record of one ``proposals.jsonl``, in file order (all versions).

    Raises:
        MineError: A line is not a valid proposal.
    """
    records = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        if line.strip():
            try:
                records.append(ScenarioProposal.model_validate_json(line))
            except ValueError as exc:
                raise MineError(f"{path}:{number}: not a valid proposal: {exc}") from exc
    return records


def proposal_files(mine_root: Path) -> list[Path]:
    """Every run's ``proposals.jsonl`` under ``<output_dir>/mine``, oldest run first."""
    return sorted(mine_root.glob(f"*/{PROPOSALS_FILE}"))


def load_queue(mine_root: Path) -> list[ScenarioProposal]:
    """The current state of every proposal (newest record per id), oldest proposal first."""
    latest: dict[str, ScenarioProposal] = {}
    for path in proposal_files(mine_root):
        for record in read_proposals(path):
            latest[record.id] = record
    return sorted(latest.values(), key=lambda proposal: proposal.id)


def queued_scenarios(mine_root: Path) -> list[Scenario]:
    """Scenarios of proposals still proposed or approved, for deduplicating new ones."""
    return [
        proposal.scenario
        for proposal in load_queue(mine_root)
        if proposal.status is not ProposalStatus.REJECTED
    ]


def decide(
    mine_root: Path,
    proposal_id: str,
    status: ProposalStatus,
    *,
    reviewer: str,
    note: str | None = None,
    scenarios_dir: Path | None = None,
) -> ScenarioProposal:
    """Record a review: append a decided copy of the proposal to the file it came from.

    Approving with ``scenarios_dir`` also writes the scenario there as
    ``<scenario id>.yaml``, which adds it to the suite.

    Raises:
        MineError: No such proposal, it was already decided, or ``status`` is ``proposed``.
        FileExistsError: The approved scenario's file already exists in ``scenarios_dir``.
    """
    if status is ProposalStatus.PROPOSED:
        raise MineError("a decision is approved or rejected")
    path, current = _find(mine_root, proposal_id)
    if current.status is not ProposalStatus.PROPOSED:
        raise MineError(f"proposal {proposal_id} was already {current.status.value}")
    decided = ScenarioProposal.model_validate(
        {
            **current.model_dump(),
            "status": status,
            "reviewer": reviewer,
            "note": note,
            "decided_at": datetime.now(UTC),
        }
    )
    if status is ProposalStatus.APPROVED and scenarios_dir is not None:
        scenarios_dir.mkdir(parents=True, exist_ok=True)
        target = scenarios_dir / f"{decided.scenario.id}.yaml"
        with target.open("x", encoding="utf-8") as handle:
            handle.write(scenario_to_yaml(decided.scenario))
    append_proposals(path, [decided])
    return decided


def _find(mine_root: Path, proposal_id: str) -> tuple[Path, ScenarioProposal]:
    for path in proposal_files(mine_root):
        records = [record for record in read_proposals(path) if record.id == proposal_id]
        if records:
            return path, records[-1]
    raise MineError(f"no proposal {proposal_id} under {mine_root}")


def read_summary(directory: Path) -> MineSummary:
    """A mining run's ``summary.json``."""
    return MineSummary.model_validate_json((directory / SUMMARY_FILE).read_text("utf-8"))
