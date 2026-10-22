"""The miner's output for review: failure clusters and the scenario approval queue (US-08).

Contract with ``hecklr-mine`` (ADR 0016a): under ``<output_dir>/mine/``, at any depth,
``clusters.jsonl`` holds ``FailureCluster`` records, ``proposals.jsonl`` ``ScenarioProposal``
records and ``conversations.jsonl`` (optional) scrubbed ``Conversation`` records, one JSON object a
line, append-only; the newest record of an id wins. A decision is appended to the proposal's own
file; an approved scenario is written to ``scenarios/mined/<id>.yaml``. Its id must be unused in
every scenario file a suite can load (``scenarios/`` and each suite's ``scenarios`` glob), since
one duplicate id breaks ``resolve_suite`` for every suite that sees both files.
"""

import tempfile
import threading
from collections.abc import Iterator
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Final

from pydantic import BaseModel, ValidationError

from hecklr.ui_api.jsonl import append_line
from hecklr_core import PERSONA_LIBRARY_GLOB, ConfigError, Project, load_model
from hecklr_schemas import (
    Conversation,
    FailureCluster,
    Persona,
    ProposalStatus,
    Scenario,
    ScenarioProposal,
)
from hecklr_sim import scenario_to_yaml

MINE_DIR: Final = "mine"
CLUSTERS_FILE: Final = "clusters.jsonl"
PROPOSALS_FILE: Final = "proposals.jsonl"
CONVERSATIONS_FILE: Final = "conversations.jsonl"
MINED_SCENARIOS: Final = Path("scenarios") / "mined"
SCENARIO_GLOB: Final = "scenarios/**/*.yaml"


class MineFileError(ValueError):
    """A line of a miner file is not the record its name promises."""


class UnknownProposalError(LookupError):
    """No proposal has this id."""


class ProposalDecidedError(RuntimeError):
    """The proposal was already approved or rejected."""


class InvalidScenarioError(ValueError):
    """The scenario to approve does not validate or does not fit the project."""

    def __init__(self, problems: list[str]) -> None:
        super().__init__("; ".join(problems))
        self.problems = problems


@dataclass(frozen=True, slots=True)
class Approval:
    """An approved proposal, the scenario file written and what the reviewer should know."""

    proposal: ScenarioProposal
    written: Path
    warnings: list[str]


@dataclass(frozen=True, slots=True)
class StoredProposal:
    """The newest record of a proposal and the file holding it."""

    proposal: ScenarioProposal
    file: Path


class MineFiles:
    """Reads the miner's files and appends review decisions to them."""

    def __init__(self, project: Project) -> None:
        self.project = project
        self.directory = project.output_dir / MINE_DIR
        self._lock = threading.Lock()

    @property
    def scenarios_dir(self) -> Path:
        """Where approved scenarios are written."""
        return self.project.root / MINED_SCENARIOS

    def clusters(self) -> list[FailureCluster]:
        """Every cluster (newest record per id), newest first."""
        newest = _newest(self._records(CLUSTERS_FILE, FailureCluster))
        return sorted(newest.values(), key=lambda c: (c.created_at, c.id), reverse=True)

    def proposals(self) -> list[StoredProposal]:
        """Every proposal (newest record per id), oldest first: the review order."""
        newest: dict[str, StoredProposal] = {}
        for path, proposal in self._records(PROPOSALS_FILE, ScenarioProposal):
            newest[proposal.id] = StoredProposal(proposal, path)
        return sorted(newest.values(), key=lambda s: (s.proposal.created_at, s.proposal.id))

    def conversations(self) -> dict[str, Conversation]:
        """Scrubbed production conversations the miner saved, by id."""
        return {c.id: c for _, c in self._records(CONVERSATIONS_FILE, Conversation)}

    def proposal(self, proposal_id: str) -> StoredProposal:
        """The newest record of one proposal.

        Raises:
            UnknownProposalError: no such proposal.
        """
        for stored in self.proposals():
            if stored.proposal.id == proposal_id:
                return stored
        raise UnknownProposalError(f"no proposal {proposal_id!r}")

    def reject(
        self, proposal_id: str, *, reviewer: str, note: str | None, now: datetime
    ) -> ScenarioProposal:
        """Record a rejection.

        Raises:
            UnknownProposalError: no such proposal.
            ProposalDecidedError: it was already decided.
        """
        with self._lock:
            stored = self._undecided(proposal_id)
            decided = _decide(stored.proposal, ProposalStatus.REJECTED, reviewer, note, now)
            _append(stored.file, decided)
        return decided

    def approve(
        self,
        proposal_id: str,
        *,
        reviewer: str,
        note: str | None,
        now: datetime,
        yaml_text: str | None,
    ) -> Approval:
        """Validate the (optionally edited) scenario, write its file and record the approval.

        Raises:
            UnknownProposalError: no such proposal.
            ProposalDecidedError: it was already decided.
            InvalidScenarioError: the scenario is invalid or clashes with the project.
        """
        with self._lock:
            stored = self._undecided(proposal_id)
            original = stored.proposal.scenario
            text = yaml_text if yaml_text is not None else scenario_to_yaml(original)
            scenario = self._validate(text)
            decided = _decide(
                stored.proposal, ProposalStatus.APPROVED, reviewer, note, now, scenario=scenario
            )
            target = self.scenarios_dir / f"{scenario.id}.yaml"
            self.scenarios_dir.mkdir(parents=True, exist_ok=True)
            # The file first: a crash before the decision is appended leaves the proposal open
            # with its scenario on disk (a retry names the existing file), never a lost scenario.
            with target.open("x", encoding="utf-8") as out:
                out.write(text)
            _append(stored.file, decided)
        return Approval(decided, target, self._unsuited_warnings(target))

    def _undecided(self, proposal_id: str) -> StoredProposal:
        stored = self.proposal(proposal_id)
        if stored.proposal.status is not ProposalStatus.PROPOSED:
            raise ProposalDecidedError(
                f"proposal {proposal_id} was already {stored.proposal.status.value} "
                f"by {stored.proposal.reviewer}"
            )
        return stored

    def _validate(self, text: str) -> Scenario:
        scenario = _parse(text)
        problems = []
        world = self.project.world.id
        if scenario.world != world:
            problems.append(f"world {scenario.world!r} is not the project's world {world!r}")
        personas = self._persona_ids()
        unknown = [p for p in scenario.persona_ids if p not in personas]
        if unknown:
            problems.append(f"unknown persona(s) {', '.join(unknown)}: not in personas/")
        if scenario.id in self._scenario_ids():
            problems.append(f"a scenario with id {scenario.id!r} already exists")
        if (self.scenarios_dir / f"{scenario.id}.yaml").exists():
            problems.append(f"{MINED_SCENARIOS / scenario.id}.yaml already exists")
        if problems:
            raise InvalidScenarioError(problems)
        return scenario

    def _persona_ids(self) -> set[str]:
        # Only the library: a scenario's own persona ids resolve there and nowhere else
        # (``resolve_suite``); a suite's ``personas`` glob replaces them instead of extending it.
        return {
            load_model(path, Persona).id for path in self.project.root.glob(PERSONA_LIBRARY_GLOB)
        }

    def _scenario_globs(self) -> list[str]:
        suites = self.project.config.suites.values()
        return sorted({SCENARIO_GLOB, *(suite.scenarios for suite in suites)})

    def _scenario_ids(self) -> set[str]:
        ids = set()
        for path in _files(self.project.root, self._scenario_globs()):
            try:
                ids.add(load_model(path, Scenario).id)
            except ConfigError:
                # Another broken scenario file is the suite loader's to report, not a reason to
                # refuse this approval; its id simply cannot clash.
                continue
        return ids

    def _unsuited_warnings(self, written: Path) -> list[str]:
        """A warning when no configured suite would run the scenario just written."""
        suites = self.project.config.suites
        runs_it = [
            name
            for name, suite in suites.items()
            if written.resolve() in _files(self.project.root, [suite.scenarios])
        ]
        if runs_it:
            return []
        globs = ", ".join(f"{name}: {suite.scenarios}" for name, suite in sorted(suites.items()))
        return [
            f"no suite runs {written.relative_to(self.project.root).as_posix()} "
            f"(suite scenario globs: {globs}); add it to a suite's glob in hecklr.yaml"
        ]

    def _records[M: BaseModel](self, name: str, model: type[M]) -> Iterator[tuple[Path, M]]:
        for path in sorted(self.directory.rglob(name)):
            with path.open(encoding="utf-8") as lines:
                for number, line in enumerate(lines, start=1):
                    if not line.strip():
                        continue
                    try:
                        yield path, model.model_validate_json(line)
                    except ValidationError as exc:
                        raise MineFileError(
                            f"{path}:{number}: not a {model.__name__}: {exc.errors()[0]['msg']}"
                        ) from exc


def _files(root: Path, globs: list[str]) -> set[Path]:
    """The files the globs match under ``root``, each once however many globs match it."""
    return {path.resolve() for pattern in globs for path in root.glob(pattern) if path.is_file()}


def _newest(records: Iterator[tuple[Path, FailureCluster]]) -> dict[str, FailureCluster]:
    return {cluster.id: cluster for _, cluster in records}


def _parse(text: str) -> Scenario:
    """Validate scenario YAML with the suite loader, so errors carry the same file and line."""
    with tempfile.TemporaryDirectory(prefix="hecklr-ui-") as scratch:
        path = Path(scratch) / "scenario.yaml"
        path.write_text(text, encoding="utf-8")
        try:
            return load_model(path, Scenario)
        except ConfigError as exc:
            raise InvalidScenarioError(
                [f"line {i.line}: {i.message}" if i.line else i.message for i in exc.issues]
            ) from exc


def _decide(
    proposal: ScenarioProposal,
    status: ProposalStatus,
    reviewer: str,
    note: str | None,
    now: datetime,
    *,
    scenario: Scenario | None = None,
) -> ScenarioProposal:
    """The decided record, validated before anything is written: its invariants must hold."""
    data = proposal.model_dump()
    data.update(status=status, reviewer=reviewer, note=note or None, decided_at=now)
    if scenario is not None:
        data["scenario"] = scenario
    return ScenarioProposal.model_validate(data)


def _append(path: Path, proposal: ScenarioProposal) -> None:
    append_line(path, proposal.model_dump_json())
