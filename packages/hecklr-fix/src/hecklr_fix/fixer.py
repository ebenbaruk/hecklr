"""The fixer pipeline (spec, "Fixer"): baseline, candidates, targeted pass, guard pass, winner.

1. Targeted baseline: the current agent on the cluster's scenarios. If it already passes them all
   there is nothing to fix; otherwise its failing trials are the generator's evidence.
2. Generate ``candidates`` patches (``generate``), restricted to the editable files.
3. Targeted pass: each candidate on the cluster's scenarios; kept on a significant improvement.
4. Guard pass: the current agent once, then each survivor, on the guard suite; dropped on a
   significant regression of the suite or any tag.
5. The winner is the accepted candidate with the largest targeted improvement.

Every run shares one seed, so baseline and candidate trials pair cell by cell (``validation``),
and one budget: Hecklr's spend (generation and every run) never exceeds ``budget_usd``. Running
out is an outcome, not a crash: the result says what was and was not evaluated.
"""

import shutil
from collections.abc import Callable, Mapping, Sequence
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from hecklr_core import BudgetEstimateError
from hecklr_fix.agent import free_port
from hecklr_fix.editable import EditableFiles
from hecklr_fix.errors import FixError
from hecklr_fix.generate import Candidate, RejectedProposal, failure_evidence, generate_candidates
from hecklr_fix.target import FixTarget
from hecklr_fix.validation import (
    SuiteRun,
    cell_outcomes,
    compare_cells,
    improved,
    regressions,
)
from hecklr_fix.workspace import TARGETED_SUITE, prepare_workspace
from hecklr_llm import PRICES, UnknownPriceError, price_for
from hecklr_schemas import (
    Comparison,
    FailureCluster,
    LLMProvider,
    ModelPrice,
    Patch,
    PatchId,
    PatchStatus,
    RunId,
    RunStatus,
    StatsConfig,
    new_id,
)


class FixOutcome(StrEnum):
    """How a fix session ended."""

    FIXED = "fixed"
    """A candidate won both passes: its diff is ready for a human to review and merge."""
    NO_CANDIDATE_BEAT_BASELINE = "no_candidate_beat_baseline"
    """Every candidate was invalid, did not improve the cluster, or regressed the suite."""
    BASELINE_PASSES = "baseline_passes"
    """The current agent passes every trial of the cluster's scenarios: nothing to fix."""
    BUDGET_EXHAUSTED = "budget_exhausted"
    """The budget ran out before any candidate was accepted."""


@dataclass(frozen=True, slots=True)
class PassRequest:
    """One run of one workspace copy."""

    config_path: Path
    suite: str
    trials: int | None
    seed: int
    budget_usd: Decimal | None
    """What is left of the session's budget."""
    output_dir: Path
    """Shared by every run of every session: one result store and one response cache, kept
    apart from the project's own runs so ``hecklr gate`` never picks a candidate's run."""


class SuiteRunner(Protocol):
    """Runs a suite of a project and returns the stored results (``hecklr run`` in-process)."""

    async def __call__(self, request: PassRequest) -> SuiteRun:
        """Run ``request.suite`` of ``request.config_path``."""
        ...


class AgentLauncher(Protocol):
    """Starts the agent of a workspace copy (``agent.AgentCommand``)."""

    def launch(
        self, workspace: Path, host: str, port: int, log: Path
    ) -> AbstractAsyncContextManager[None]:
        """Run the agent from ``workspace`` on ``port`` for the duration of the block."""
        ...


@dataclass(frozen=True, slots=True)
class FixSettings:
    """Knobs of one session (``hecklr fix`` flags)."""

    model: str
    """``provider/model`` that writes the candidate patches."""
    guard_suite: str
    seed: int
    stats: StatsConfig
    candidates: int = 5
    targeted_trials: int = 3
    guard_trials: int | None = None
    """Trials per guard-suite scenario; the suite's own when ``None``."""
    budget_usd: Decimal | None = None
    """Cap on the session's spend. As for ``hecklr run``, spend may reach it but not cross it, so
    at the cap (at once, for $0) only free models run: ``fake/``, ``ollama/``, or priced at 0."""
    prices: Mapping[str, ModelPrice] = field(default_factory=dict)
    """``models.prices`` of ``hecklr.yaml``, over ``hecklr_llm.PRICES``: which models are free."""
    evidence_trials: int = 6
    """Failing trials shown to the generator."""


@dataclass(frozen=True, slots=True)
class FixResult:
    """What a session found."""

    fix_id: str
    outcome: FixOutcome
    message: str
    cluster: FailureCluster
    patches: list[Patch]
    """Every candidate that applied, in candidate order, with its final status."""
    rejected: list[RejectedProposal]
    """Proposals dropped before any run (guardrails, unusable replies, duplicates)."""
    winner: Patch | None
    targeted_baseline: RunId | None
    guard_baseline: RunId | None
    spent_usd: Decimal
    """Hecklr's spend: generation plus every run. The agent's own model calls are not in it."""
    winner_files: Mapping[str, str] = field(default_factory=dict)
    """The winner's new text of each file it changes (what ``--pr`` commits)."""


class _BudgetExhaustedError(Exception):
    """Internal: stop evaluating, report what is known."""


@dataclass
class _Session:
    spent: Decimal = Decimal(0)
    patches: dict[int, Patch] = field(default_factory=dict)
    files: dict[int, dict[str, str]] = field(default_factory=dict)
    """Each candidate's changed files, by candidate index."""


class Fixer:
    """One fix session over one target."""

    def __init__(
        self,
        *,
        config_path: Path,
        editable: EditableFiles,
        target: FixTarget,
        settings: FixSettings,
        llm: LLMProvider,
        runner: SuiteRunner,
        launcher: AgentLauncher,
        session_dir: Path,
        runs_output_dir: Path,
        fix_id: str | None = None,
        ports: Callable[[], int] = free_port,
    ) -> None:
        """Set up a session.

        ``session_dir`` holds the session's workspaces and agent logs; ``runs_output_dir`` its
        runs and response cache (``PassRequest.output_dir``); ``ports`` picks the port of each
        agent launch.
        """
        if settings.candidates < 1 or settings.targeted_trials < 1:
            raise FixError("the fixer needs at least one candidate and one trial per scenario")
        self.ports = ports
        self.config_path = config_path
        self.editable = editable
        self.target = target
        self.settings = settings
        self.llm = llm
        self.runner = runner
        self.launcher = launcher
        self.session_dir = session_dir
        self.runs_output_dir = runs_output_dir
        self.fix_id = fix_id or new_id()
        self._session = _Session()

    async def run(self) -> FixResult:
        """Run the whole pipeline.

        Raises:
            FixError: the agent cannot start, a run fails, or the runs cannot be compared.
            hecklr_llm.LLMError: the patch generator's provider failed.
        """
        try:
            baseline = await self._run_pass("baseline-targeted", {}, TARGETED_SUITE)
        except _BudgetExhaustedError as exc:
            return self._result(FixOutcome.BUDGET_EXHAUSTED, str(exc), [], None, None)
        outcomes = cell_outcomes(baseline)
        if not outcomes:
            raise FixError(
                f"no trial of the targeted baseline run {baseline.results.run.id} could be "
                "analyzed (harness errors?); see its results"
            )
        if all(outcome.passed for outcome in outcomes):
            return self._result(
                FixOutcome.BASELINE_PASSES,
                "the current agent passes every trial of the cluster's scenarios: nothing to fix",
                [],
                baseline,
                None,
            )
        try:
            self._check_generation_budget()
        except _BudgetExhaustedError as exc:
            return self._result(FixOutcome.BUDGET_EXHAUSTED, str(exc), [], baseline, None)
        generation = await generate_candidates(
            self.llm,
            model=self.settings.model,
            target=self.target,
            files=self.editable.contents,
            evidence=failure_evidence(baseline, max_trials=self.settings.evidence_trials),
            count=self.settings.candidates,
            seed=self.settings.seed,
        )
        self._session.spent += generation.cost_usd
        for candidate in generation.candidates:
            self._propose(candidate)
        guard_baseline: SuiteRun | None = None
        try:
            survivors = await self._targeted_pass(generation.candidates, baseline)
            if survivors:
                guard_baseline = await self._run_pass("baseline-guard", {}, self._guard_suite)
                await self._guard_pass(survivors, guard_baseline)
        except _BudgetExhaustedError as exc:
            return self._finish(generation.rejected, baseline, guard_baseline, str(exc))
        return self._finish(generation.rejected, baseline, guard_baseline, None)

    @property
    def _guard_suite(self) -> str:
        return self.settings.guard_suite

    async def _targeted_pass(
        self, candidates: Sequence[Candidate], baseline: SuiteRun
    ) -> list[Candidate]:
        survivors = []
        for candidate in candidates:
            run = await self._run_pass(f"candidate-{candidate.index}-targeted", candidate.files)
            comparisons = self._compare(baseline, run)
            status = (
                PatchStatus.PROPOSED if improved(comparisons) else PatchStatus.REJECTED_TARGETED
            )
            self._record(candidate, status, targeted=comparisons)
            if status is PatchStatus.PROPOSED:
                survivors.append(candidate)
        return survivors

    async def _guard_pass(self, survivors: Sequence[Candidate], baseline: SuiteRun) -> None:
        for candidate in survivors:
            run = await self._run_pass(
                f"candidate-{candidate.index}-guard", candidate.files, self._guard_suite
            )
            comparisons = self._compare(baseline, run, guard=True)
            status = (
                PatchStatus.REJECTED_GUARD if regressions(comparisons) else PatchStatus.ACCEPTED
            )
            self._record(candidate, status, guard=comparisons)

    def _compare(
        self, baseline: SuiteRun, candidate: SuiteRun, *, guard: bool = False
    ) -> list[Comparison]:
        return compare_cells(
            baseline, candidate, stats=self.settings.stats, seed=self.settings.seed, guard=guard
        )

    async def _run_pass(
        self, label: str, files: Mapping[str, str], suite: str = TARGETED_SUITE
    ) -> SuiteRun:
        budget = self.settings.budget_usd
        left = None if budget is None else budget - self._session.spent
        if left is not None and left < 0:
            raise _BudgetExhaustedError(f"the budget of ${budget} was spent before {label}")
        port = self.ports()
        workspace = prepare_workspace(
            config_path=self.config_path,
            dest=self.session_dir / "work" / label,
            files=files,
            port=port,
            target=self.target,
            targeted_trials=self.settings.targeted_trials,
        )
        targeted = suite == TARGETED_SUITE
        request = PassRequest(
            config_path=workspace.config_path,
            suite=suite,
            trials=self.settings.targeted_trials if targeted else self.settings.guard_trials,
            seed=self.settings.seed,
            budget_usd=left,
            output_dir=self.runs_output_dir,
        )
        try:
            log = self.session_dir / "logs" / f"{label}.log"
            async with self.launcher.launch(workspace.root, workspace.agent_host, port, log):
                run = await self.runner(request)
        except BudgetEstimateError as exc:
            raise _BudgetExhaustedError(f"{label}: {exc}") from exc
        finally:
            # The copy is only the agent's code and files; the results live in the session.
            shutil.rmtree(workspace.root, ignore_errors=True)
        status = run.results.run.status
        self._session.spent += run.results.run.spent_usd
        if status is RunStatus.BUDGET_EXCEEDED:
            raise _BudgetExhaustedError(f"the budget ran out during {label}")
        if status is not RunStatus.COMPLETED:
            raise FixError(f"run {run.results.run.id} ({label}) ended {status.value}")
        return run

    def _check_generation_budget(self) -> None:
        """Refuse generation once spend crossed the cap, or reached it with a priced model.

        A run's own pre-run estimate makes that call for its simulator and judge models; the
        generator has none, so it is made here.
        """
        budget = self.settings.budget_usd
        if budget is None:
            return
        spent = self._session.spent
        model = self.settings.model
        if spent > budget or (spent == budget and not _free(model, self.settings.prices)):
            raise _BudgetExhaustedError(
                f"the budget of ${budget} was spent before generating candidates "
                f"({model} is not a free model)"
            )

    def _propose(self, candidate: Candidate) -> None:
        self._session.files[candidate.index] = candidate.files
        self._session.patches[candidate.index] = Patch(
            id=PatchId(new_id()),
            cluster_id=self.target.cluster.id,
            candidate_index=candidate.index,
            files=sorted(candidate.files),
            diff=candidate.diff,
            rationale=candidate.rationale,
            generator_model=candidate.model,
            cost_usd=candidate.cost_usd,
            created_at=datetime.now(UTC),
        )

    def _record(self, candidate: Candidate, status: PatchStatus, **comparisons: object) -> None:
        """Update the candidate's patch with its new status and a pass's comparisons."""
        patch = self._session.patches[candidate.index]
        self._session.patches[candidate.index] = patch.model_copy(
            update={"status": status, **comparisons}
        )

    def _finish(
        self,
        rejected: list[RejectedProposal],
        baseline: SuiteRun,
        guard_baseline: SuiteRun | None,
        budget_note: str | None,
    ) -> FixResult:
        accepted = [p for p in self._session.patches.values() if p.status is PatchStatus.ACCEPTED]
        winner = max(accepted, key=_rank, default=None)
        if winner is not None:
            outcome = FixOutcome.FIXED
            message = f"candidate {winner.candidate_index} fixes the cluster and passes the guard"
            if budget_note is not None:
                message += f" (not every candidate was evaluated: {budget_note})"
        elif budget_note is not None:
            outcome, message = FixOutcome.BUDGET_EXHAUSTED, budget_note
        else:
            outcome = FixOutcome.NO_CANDIDATE_BEAT_BASELINE
            message = "no candidate beat the baseline"
        return self._result(outcome, message, rejected, baseline, guard_baseline, winner)

    def _result(
        self,
        outcome: FixOutcome,
        message: str,
        rejected: list[RejectedProposal],
        baseline: SuiteRun | None,
        guard_baseline: SuiteRun | None,
        winner: Patch | None = None,
    ) -> FixResult:
        return FixResult(
            fix_id=self.fix_id,
            outcome=outcome,
            message=message,
            cluster=self.target.cluster,
            patches=[self._session.patches[i] for i in sorted(self._session.patches)],
            rejected=rejected,
            winner=winner,
            targeted_baseline=baseline.results.run.id if baseline is not None else None,
            guard_baseline=guard_baseline.results.run.id if guard_baseline is not None else None,
            spent_usd=self._session.spent,
            winner_files=self._session.files[winner.candidate_index] if winner else {},
        )


def _free(model: str, prices: Mapping[str, ModelPrice]) -> bool:
    try:
        price = price_for(model, {**PRICES, **prices})
    except UnknownPriceError:
        return False  # an unpriced paid model fails closed, as in the pre-run estimate
    return price.input_per_mtok == 0 and price.output_per_mtok == 0


def _rank(patch: Patch) -> tuple[float, float, int]:
    targeted = patch.targeted[0].delta.value if patch.targeted else 0.0
    guard = patch.guard[0].delta.value if patch.guard else 0.0
    return (targeted, guard, -patch.candidate_index)
