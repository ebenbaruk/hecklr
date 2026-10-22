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
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, replace
from decimal import Decimal
from pathlib import Path
from typing import Final

from hecklr.channels import (
    BuiltDriver,
    make_email_driver,
    make_voice_driver,
)
from hecklr.config import load_project_config
from hecklr.testing import scripted_models
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
    open_llm,
    plan_run,
    resolve_suite,
)
from hecklr_core.storage import RUN_JSON
from hecklr_grade import build_graders, grade_trial
from hecklr_llm import ProviderRouter
from hecklr_report.outcomes import convert
from hecklr_schemas import (
    Channel,
    ChannelDriver,
    CustomCriterion,
    Event,
    Grade,
    GradeContext,
    JudgeCriterion,
    LLMProvider,
    ModelsConfig,
    Persona,
    PythonAdapterConfig,
    Run,
    RunId,
    RunStatus,
    Scenario,
    SimulationMode,
    Trial,
    is_ulid,
)
from hecklr_schemas.common import ModelName
from hecklr_sim import make_adversarial_simulator
from hecklr_stats import ScenarioCounts, TrialOutcome, allocate_trials
from hecklr_world import Sandbox

ADAPTIVE_EXTRA_SHARE: Final = 0.5
"""``--adaptive`` spends up to this share of the first pass's trials on a second pass."""

TrialCallback = Callable[[Trial, Sequence[Grade]], None]
"""Called after each trial is stored, e.g. to advance a progress bar."""


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
    releases: tuple[Callable[[], None], ...] = ()
    """Called once the run ends, e.g. to stop the email webhook's reply endpoint."""

    @property
    def project(self) -> Project:
        """The loaded project."""
        return self.plan.suite.project

    @property
    def runs_dir(self) -> Path:
        """Where this run's results directory is created."""
        return self.project.output_dir / RUNS_DIR

    @property
    def models_called(self) -> list[ModelName]:
        """The models this run can call: the ones whose providers must be usable."""
        return models_called(self.project.config.models, self.plan.suite.scenarios)


def prepare_run(request: RunRequest) -> PreparedRun:
    """Load, validate and plan a run, and build its drivers and graders; nothing runs yet.

    Every configuration problem surfaces here, before a trial starts or a model is called,
    except the model providers' setup (API keys, base URLs): ``execute_run`` checks that before
    it records the run, because providers can be injected there.

    Raises:
        hecklr_core.ConfigError: an authored file is invalid or the suite selects nothing.
        hecklr_world.WorldConfigError: the world's seed data cannot be loaded.
        hecklr_core.BudgetEstimateError: the cost estimate exceeds the budget.
        hecklr_llm.UnknownPriceError: a paid model has no known price.
        ChannelUnavailableError: a voice suite without ``hecklr-voice``, or a voice provider
            (TTS, ASR, line codec) this machine cannot run.
        hecklr_adapters.AdapterConfigError: an adapter cannot work.
        hecklr_grade.InvalidCriterionError: a success criterion can never be graded.
        UnknownBaselineError: ``baseline_id`` is not a stored run.
    """
    project = load_run_project(
        request.config_path, seed=request.seed, output_dir=request.output_dir
    )
    suite = resolve_suite(project, request.suite, trials=request.trials, channels=request.channels)
    baseline = _check_baseline(project, request.baseline_id)
    grader = make_trial_grader(suite.scenarios)
    plan = plan_run(
        suite,
        budget_usd=request.budget_usd,
        agent_version=request.agent_version,
        git_sha=detect_git_sha(project.root),
        baseline_id=baseline,
    )
    channels = {channel for cell in suite.cells for channel in cell.channels}
    run_dir = project.output_dir / RUNS_DIR / plan.run.id
    built = build_drivers(project, channels, personas=suite.personas.values(), run_dir=run_dir)
    adaptive = suite.settings.adaptive if request.adaptive is None else request.adaptive
    return PreparedRun(
        plan=plan,
        drivers={channel: entry.driver for channel, entry in built.items()},
        grade_trial=grader,
        adaptive=adaptive,
        releases=tuple(entry.close for entry in built.values() if entry.close is not None),
    )


def load_run_project(
    config_path: Path, *, seed: int | None = None, output_dir: Path | None = None
) -> Project:
    """The project with the world's seed files inlined and CLI overrides applied.

    ``hecklr.yaml`` may name the ``websocket`` voice adapter (RFC 0010a, ``hecklr.config``).
    """
    project = load_project_config(config_path)
    updates: dict[str, object] = {}
    if seed is not None:
        updates["seed"] = seed
    if output_dir is not None:
        # An absolute path survives ``root / output_dir`` unchanged; a relative one is taken from
        # the current directory, as a command-line path should be.
        updates["output_dir"] = str(output_dir.resolve())
    return replace(project, config=project.config.model_copy(update=updates))


def make_trial_grader(scenarios: Mapping[str, Scenario]) -> TrialGrader:
    """Grading through ``hecklr_grade``: graders built once per scenario, run by ``grade_trial``.

    Building every scenario's graders here makes a bad regex, state expression, rubric or custom
    grader fail before the run starts instead of in the middle of it.
    """
    graders = {scenario_id: build_graders(scenario) for scenario_id, scenario in scenarios.items()}

    async def grade(ctx: GradeContext) -> list[Grade]:
        return await grade_trial(ctx, graders[ctx.scenario.id])

    return grade


def build_drivers(
    project: Project, channels: set[Channel], *, personas: Iterable[Persona], run_dir: Path
) -> dict[Channel, BuiltDriver]:
    """One driver per channel the run uses, each bound to the agent's adapter for it.

    ``personas`` are checked for what the voice channel needs; voice recordings go under
    ``run_dir``. ``resolve_suite`` already refused a channel without an adapter.
    """
    adapters = project.config.agent.adapters
    drivers: dict[Channel, BuiltDriver] = {}
    if Channel.CHAT in channels and adapters.chat is not None:
        if isinstance(adapters.chat, PythonAdapterConfig):
            _make_importable(project.root)
        drivers[Channel.CHAT] = BuiltDriver(ChatDriver(make_chat_target(adapters.chat)))
    if Channel.VOICE in channels and adapters.voice is not None:
        drivers[Channel.VOICE] = make_voice_driver(
            adapters.voice,
            personas,
            asr_model=project.config.models.asr,
            output_dir=project.output_dir,
            run_dir=run_dir,
        )
    if Channel.EMAIL in channels and adapters.email is not None:
        drivers[Channel.EMAIL] = make_email_driver(adapters.email)
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
        llm_providers: Provider instances per model prefix (tests inject ``FakeLLM``); by
            default ``fake/`` models are ``hecklr.testing.scripted_models`` (ADR 0007c).
        on_trial: Called after each trial is stored.
        on_adaptive: Called with the number of extra trials before an adaptive pass starts.

    Raises:
        hecklr_llm.ProviderConfigError: a model's provider lacks its API key or base URL; raised
            before the run is recorded.
        hecklr_llm.UnknownProviderError: a model names an unknown provider; idem.
    """
    project = prepared.project
    config = project.config
    if llm_providers is None:
        llm_providers = scripted_models()
    await check_model_providers(config.models, prepared.models_called, llm_providers)
    try:
        return await _execute(prepared, llm_providers, on_trial, on_adaptive)
    finally:
        for release in prepared.releases:
            release()


async def _execute(
    prepared: PreparedRun,
    llm_providers: Mapping[str, LLMProvider],
    on_trial: TrialCallback | None,
    on_adaptive: Callable[[int], None] | None,
) -> RunResults:
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
            simulator_factory=make_adversarial_simulator(config.models.simulator),
            simulator_llm=llm,
            judge_llm=llm,
            grade_trial=prepared.grade_trial,
        )
        executor = RunExecutor(prepared.plan.suite, collaborators, store)
        run = await executor.execute(prepared.plan.run)
        if prepared.adaptive and run.status is RunStatus.COMPLETED:
            suite = prepared.plan.suite
            reader = _OutcomeReader(store, suite.scenarios, suite.personas)
            run = await _adaptive_pass(executor, reader, run, on_adaptive)
        return await store.load_run(run.id)


async def check_model_providers(
    models: ModelsConfig,
    called: Sequence[ModelName],
    llm_providers: Mapping[str, LLMProvider] | None = None,
) -> None:
    """Set up the provider of every model in ``called`` once, so a missing key fails fast.

    Without this, a missing API key surfaces as a harness error in every trial of a recorded
    run. Creating a provider makes no network call.
    """
    async with ProviderRouter(
        models.providers, prices=models.prices, providers=llm_providers
    ) as router:
        for model in called:
            router.provider_for(model)


def models_called(models: ModelsConfig, scenarios: Mapping[str, Scenario]) -> list[ModelName]:
    """The models a run of ``scenarios`` can call, in first-seen order (issue #19).

    The simulator only for scenarios that are not scripted; the judge model for judge criteria
    without a ``model`` of their own and for custom graders (which may call the judge LLM); each
    judge criterion's own ``model``. A scripted suite graded deterministically calls none, so it
    needs no API key.
    """
    called: list[ModelName] = []
    for scenario in scenarios.values():
        if scenario.mode is not SimulationMode.SCRIPTED:
            called.append(models.simulator)
        for criterion in scenario.success:
            if isinstance(criterion, JudgeCriterion):
                called.append(criterion.model or models.judge)
            elif isinstance(criterion, CustomCriterion):
                called.append(models.judge)
    return list(dict.fromkeys(called))


@dataclass(frozen=True, slots=True)
class _OutcomeReader:
    """Reads a stored run as statistics outcomes, by ``hecklr-report``'s trial verdict rule."""

    store: ResultStore
    scenarios: Mapping[str, Scenario]
    personas: Mapping[str, Persona]

    async def outcomes(self, run_id: str) -> list[TrialOutcome]:
        """The analyzable trials of run ``run_id``."""
        results = await self.store.load_run(run_id)
        return convert(results, self.scenarios, self.personas).outcomes


async def _adaptive_pass(
    executor: RunExecutor,
    reader: _OutcomeReader,
    run: Run,
    on_adaptive: Callable[[int], None] | None,
) -> Run:
    """Spend extra trials where they most narrow the comparison with the baseline (ADR 0007a).

    Without a baseline the run is compared with itself, so no scenario counts as flipped and the
    extra trials go to the widest intervals.
    """
    first_pass = await reader.outcomes(run.id)
    if not first_pass:
        return run
    candidate = ScenarioCounts.from_outcomes(first_pass)
    baseline = candidate
    if run.baseline_id is not None:
        stored = await reader.outcomes(run.baseline_id)
        if stored:
            baseline = ScenarioCounts.from_outcomes(stored)
    budget = math.ceil(ADAPTIVE_EXTRA_SHARE * len(first_pass))
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
