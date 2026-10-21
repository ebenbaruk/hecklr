"""Composition: turn ``hecklr.yaml`` and a suite into a running, stored, graded run.

This is the one place that knows every concrete component. The orchestrator (``hecklr-core``)
talks only to Protocols; here they are bound to the world sandbox (``hecklr-world``), the channel
drivers and adapters (``hecklr-channels``, ``hecklr-adapters``), the simulator (``hecklr-sim``),
the graders (``hecklr-grade``) and the model stack (``hecklr_core.open_llm``).

Two steps, so the CLI can show the plan (and refuse it) before anything starts::

    prepared = prepare_run(RunRequest(config_path=Path("hecklr.yaml"), suite="ci"))
    results = await execute_run(prepared)
"""

import math
import os
import subprocess
import sys
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Final

from hecklr.outcomes import trial_outcomes
from hecklr_adapters import make_chat_target
from hecklr_channels import ChatDriver
from hecklr_core import (
    RUNS_DIR,
    Collaborators,
    Project,
    ResultStore,
    RunExecutor,
    RunPlan,
    RunResults,
    TrialGrader,
    extend_matrix,
    load_project,
    open_llm,
    plan_run,
    resolve_suite,
)
from hecklr_core.storage import RUN_JSON
from hecklr_grade import build_graders, grade_trial
from hecklr_schemas import (
    Channel,
    ChannelDriver,
    Event,
    Grade,
    GradeContext,
    LLMProvider,
    PythonAdapterConfig,
    Run,
    RunId,
    RunStatus,
    Scenario,
    Trial,
    is_ulid,
)
from hecklr_sim import make_simulator
from hecklr_stats import ScenarioCounts, allocate_trials
from hecklr_world import Sandbox, load_world

ADAPTIVE_EXTRA_SHARE: Final = 0.5
"""``--adaptive`` spends up to this share of the first pass's trials on a second pass."""

_UNAVAILABLE_CHANNELS: Final[Mapping[Channel, str]] = {
    Channel.VOICE: (
        "the voice channel driver is not available in this version yet (it ships with the "
        "hecklr[voice] extra)"
    ),
    Channel.EMAIL: "the email channel driver is not available in this version yet",
}

TrialCallback = Callable[[Trial, Sequence[Grade]], None]
"""Called after each trial is stored, e.g. to advance a progress bar."""


class ChannelUnavailableError(Exception):
    """The suite needs a channel whose driver is not installed or not built yet."""


class UnknownBaselineError(LookupError):
    """``--baseline`` names a run that is not stored under the project's output directory."""


@dataclass(frozen=True, slots=True)
class RunRequest:
    """What to run: ``hecklr run`` flags, or the arguments of ``hecklr.run``."""

    config_path: Path
    suite: str
    channels: Sequence[Channel] | None = None
    """Restrict to these channels (``--channels``); the suite's own if ``None``."""
    trials: int | None = None
    budget_usd: Decimal | None = None
    seed: int | None = None
    """Root seed override (``--seed``); ``hecklr.yaml``'s ``seed`` if ``None``."""
    adaptive: bool | None = None
    """Run an adaptive second pass; the suite's ``adaptive`` setting if ``None``."""
    baseline_id: str | None = None
    """A stored run to record as this run's baseline, for later comparison."""
    output_dir: Path | None = None
    """Where runs and the response cache go; ``hecklr.yaml``'s ``output_dir`` if ``None``."""
    agent_version: str | None = None


@dataclass(frozen=True, slots=True)
class PreparedRun:
    """A validated, planned run whose components are built but not started."""

    plan: RunPlan
    drivers: Mapping[Channel, ChannelDriver]
    grade_trial: TrialGrader
    adaptive: bool

    @property
    def project(self) -> Project:
        """The loaded project."""
        return self.plan.suite.project

    @property
    def runs_dir(self) -> Path:
        """Where this run's results directory is created."""
        return self.project.output_dir / RUNS_DIR


def prepare_run(request: RunRequest) -> PreparedRun:
    """Load, validate and plan a run, and build its drivers and graders; nothing runs yet.

    Every configuration problem surfaces here, before a trial starts or a model is called.

    Raises:
        hecklr_core.ConfigError: an authored file is invalid or the suite selects nothing.
        hecklr_world.WorldConfigError: the world's seed data cannot be loaded.
        hecklr_core.BudgetEstimateError: the cost estimate exceeds the budget.
        hecklr_llm.UnknownPriceError: a paid model has no known price.
        ChannelUnavailableError: the suite needs a voice or email driver.
        hecklr_adapters.AdapterConfigError: the chat adapter cannot work.
        hecklr_grade.InvalidCriterionError: a success criterion can never be graded.
        UnknownBaselineError: ``baseline_id`` is not a stored run.
    """
    project = load_run_project(
        request.config_path, seed=request.seed, output_dir=request.output_dir
    )
    suite = resolve_suite(project, request.suite, trials=request.trials, channels=request.channels)
    baseline = _check_baseline(project, request.baseline_id)
    grader = make_trial_grader(suite.scenarios)
    channels = {channel for cell in suite.cells for channel in cell.channels}
    drivers = build_drivers(project, channels)
    plan = plan_run(
        suite,
        budget_usd=request.budget_usd,
        agent_version=request.agent_version,
        git_sha=detect_git_sha(project.root),
        baseline_id=baseline,
    )
    adaptive = suite.settings.adaptive if request.adaptive is None else request.adaptive
    return PreparedRun(plan=plan, drivers=drivers, grade_trial=grader, adaptive=adaptive)


def load_run_project(
    config_path: Path, *, seed: int | None = None, output_dir: Path | None = None
) -> Project:
    """``load_project`` with the world's seed files inlined and CLI overrides applied."""
    project = load_project(config_path)
    updates: dict[str, object] = {}
    if seed is not None:
        updates["seed"] = seed
    if output_dir is not None:
        # An absolute path survives ``root / output_dir`` unchanged; a relative one is taken from
        # the current directory, as a command-line path should be.
        updates["output_dir"] = str(output_dir.resolve())
    world = load_world(project.root / project.config.world)
    return replace(project, config=project.config.model_copy(update=updates), world=world)


def make_trial_grader(scenarios: Mapping[str, Scenario]) -> TrialGrader:
    """Grading through ``hecklr_grade``: graders built once per scenario, run by ``grade_trial``.

    Building every scenario's graders here makes a bad regex, state expression, rubric or custom
    grader fail before the run starts instead of in the middle of it.
    """
    graders = {scenario_id: build_graders(scenario) for scenario_id, scenario in scenarios.items()}

    async def grade(ctx: GradeContext) -> list[Grade]:
        return await grade_trial(ctx, graders[ctx.scenario.id])

    return grade


def build_drivers(project: Project, channels: set[Channel]) -> dict[Channel, ChannelDriver]:
    """One driver per channel the run uses, each bound to the agent's adapter for it."""
    missing = sorted(channel.value for channel in channels if channel in _UNAVAILABLE_CHANNELS)
    if missing:
        reasons = "; ".join(_UNAVAILABLE_CHANNELS[Channel(name)] for name in missing)
        raise ChannelUnavailableError(
            f"this suite runs on {missing}, but {reasons}. "
            "Run the chat channel only with `--channels chat`."
        )
    drivers: dict[Channel, ChannelDriver] = {}
    chat = project.config.agent.adapters.chat
    if Channel.CHAT in channels and chat is not None:
        if isinstance(chat, PythonAdapterConfig):
            _make_importable(project.root)
        drivers[Channel.CHAT] = ChatDriver(make_chat_target(chat))
    return drivers


def _make_importable(root: Path) -> None:
    # A python adapter's entry point is a module of the user's project; `hecklr run` must find it
    # as `python -m` would from the project directory, whatever directory the CLI started in.
    location = str(root)
    if location not in sys.path:
        sys.path.insert(0, location)


def detect_git_sha(root: Path) -> str | None:
    """The commit being tested: ``GITHUB_SHA`` in Actions, else ``HEAD`` of the project's repo."""
    from_ci = os.environ.get("GITHUB_SHA")
    if from_ci:
        return from_ci
    try:
        completed = subprocess.run(
            ["git", "rev-parse", "HEAD"],  # noqa: S607 -- git is resolved on PATH like any CLI tool
            cwd=root,
            capture_output=True,
            text=True,
            check=True,
            timeout=5,
        )
    except (OSError, subprocess.SubprocessError):
        # Not a repository, or git is not installed: the run simply records no commit.
        return None
    return completed.stdout.strip() or None


def _check_baseline(project: Project, baseline_id: str | None) -> RunId | None:
    if baseline_id is None:
        return None
    runs_dir = project.output_dir / RUNS_DIR
    if not is_ulid(baseline_id) or not (runs_dir / baseline_id / RUN_JSON).is_file():
        raise UnknownBaselineError(f"no stored run {baseline_id!r} under {runs_dir}")
    return RunId(baseline_id)


class _ReportingStore(ResultStore):
    """A ``ResultStore`` that reports each stored trial, for live progress."""

    def __init__(self, runs_dir: Path, on_trial: TrialCallback | None) -> None:
        super().__init__(runs_dir)
        self._on_trial = on_trial

    async def append_trial_result(
        self, trial: Trial, events: Sequence[Event], grades: Sequence[Grade]
    ) -> None:
        """Store the trial, then report it."""
        await super().append_trial_result(trial, events, grades)
        if self._on_trial is not None:
            self._on_trial(trial, grades)


async def execute_run(
    prepared: PreparedRun,
    *,
    llm_providers: Mapping[str, LLMProvider] | None = None,
    on_trial: TrialCallback | None = None,
    on_adaptive: Callable[[int], None] | None = None,
) -> RunResults:
    """Run the planned trials (plus an adaptive pass if asked) and return the stored results.

    Args:
        prepared: From ``prepare_run``.
        llm_providers: Provider instances per model prefix (tests inject ``FakeLLM``).
        on_trial: Called after each trial is stored.
        on_adaptive: Called with the number of extra trials before an adaptive pass starts.
    """
    project = prepared.project
    config = project.config
    async with (
        open_llm(config, project.output_dir, providers=llm_providers) as llm,
        Sandbox(tool_mode=config.agent.tool_mode) as sandbox,
        _ReportingStore(prepared.runs_dir, on_trial) as store,
    ):
        collaborators = Collaborators(
            sandbox=sandbox,
            drivers=prepared.drivers,
            simulator_factory=make_simulator(config.models.simulator),
            simulator_llm=llm,
            judge_llm=llm,
            grade_trial=prepared.grade_trial,
        )
        executor = RunExecutor(prepared.plan.suite, collaborators, store)
        run = await executor.execute(prepared.plan.run)
        if prepared.adaptive and run.status is RunStatus.COMPLETED:
            run = await _adaptive_pass(executor, store, run, on_adaptive)
        return await store.load_run(run.id)


async def _adaptive_pass(
    executor: RunExecutor,
    store: ResultStore,
    run: Run,
    on_adaptive: Callable[[int], None] | None,
) -> Run:
    """Spend extra trials where they most narrow the comparison with the baseline (ADR 0007a).

    Without a baseline the run is compared with itself, so no scenario counts as flipped and the
    extra trials go to the widest intervals.
    """
    first_pass = trial_outcomes(await store.load_run(run.id))
    if not first_pass.outcomes:
        return run
    candidate = ScenarioCounts.from_outcomes(first_pass.outcomes)
    baseline = candidate
    if run.baseline_id is not None:
        stored = trial_outcomes(await store.load_run(run.baseline_id))
        if stored.outcomes:
            baseline = ScenarioCounts.from_outcomes(stored.outcomes)
    budget = math.ceil(ADAPTIVE_EXTRA_SHARE * len(first_pass.outcomes))
    allocation = allocate_trials(baseline, candidate, budget)
    specs = [
        spec
        for extra in sorted(set(allocation.values()))
        for spec in extend_matrix(
            run.matrix,
            run.seed,
            [scenario for scenario, count in allocation.items() if count == extra],
            extra,
        )
    ]
    if not specs:
        return run
    if on_adaptive is not None:
        on_adaptive(len(specs))
    return await executor.run_additional(run, specs)
