"""Measure one stack: start its agent, run the track's suite against it, write its row.

Every stack runs the same suite with the same seed, models and trial counts, one conversation at
a time: the benchmark measures the stack, not how it shares a laptop with itself, and running
trials one by one lets the agent's own spend be read per conversation from its ``spend_url``.
"""

import hashlib
import os
import platform
import sys
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from functools import partial
from importlib import metadata
from pathlib import Path
from typing import Final

import anyio
import yaml

from hecklr import __version__
from hecklr.commands.bench.process import (
    AgentStatus,
    StackError,
    agent_process,
    free_port,
    read_status,
)
from hecklr.commands.bench.result import Environment, Spend, StackResult, stack_metrics
from hecklr.commands.bench.spec import (
    MANIFEST,
    PORTS,
    Manifest,
    StackSpec,
    Track,
    fill,
    fill_json,
)
from hecklr.compose import (
    RunRequest,
    check_model_providers,
    detect_git_sha,
    execute_run,
    prepare_run,
)
from hecklr.results import analyze_results
from hecklr_core import Project, RunResults
from hecklr_schemas import Channel, Grade, LLMProvider, RunStatus, Trial

CONCURRENCY: Final = 1
PACKAGES: Final = (
    "pipecat-ai",
    "livekit",
    "faster-whisper",
    "kokoro-onnx",
    "onnxruntime",
    "aiortc",
    "websockets",
)
CHANNELS: Final[Mapping[Track, Channel]] = {
    "chat": Channel.CHAT,
    "voice": Channel.VOICE,
    "calibration": Channel.VOICE,
}

TrialReport = Callable[[Trial, Decimal], None]
"""Called after each conversation with the agent's spend during it."""


@dataclass(frozen=True, slots=True)
class BenchPaths:
    """Where the benchmark lives and where a run may write."""

    bench_dir: Path
    work_dir: Path
    """Per-stack project copies, result stores, response caches and agent logs."""

    @property
    def repo(self) -> Path:
        """The checkout holding ``bench/``: the agents' working directory and ``{repo}``."""
        return self.bench_dir.resolve().parent

    @property
    def suite_dir(self) -> Path:
        """The suite of ``bench.yaml``."""
        return self.bench_dir / "suite"


def suite_digest(bench_dir: Path, manifest: Manifest) -> str:
    """SHA-256 over ``bench.yaml`` and every suite file (path and bytes), in path order."""
    digest = hashlib.sha256((bench_dir / MANIFEST).read_bytes())
    suite = bench_dir / manifest.suite
    for path in sorted(p for p in suite.rglob("*") if p.is_file()):
        digest.update(path.relative_to(suite).as_posix().encode())
        digest.update(path.read_bytes())
    return digest.hexdigest()


def write_project(
    manifest: Manifest,
    stack: StackSpec,
    *,
    suite_dir: Path,
    project_dir: Path,
    values: Mapping[str, str],
) -> Path:
    """Copy the suite into ``project_dir`` with a ``hecklr.yaml`` for ``stack``; its path."""
    _copy_tree(suite_dir, project_dir)
    track = manifest.tracks[stack.track]
    channel = CHANNELS[stack.track].value
    models: dict[str, object] = manifest.models.model_dump()
    if track.asr is not None:
        models["asr"] = track.asr
    config = {
        "version": 1,
        "agent": {"name": stack.id, "adapters": {channel: fill_json(stack.adapter, values)}},
        "world": manifest.world,
        "suites": {
            stack.track: {
                "scenarios": track.scenarios,
                "trials": track.trials,
                "channels": [channel],
            }
        },
        "models": models,
        "stats": manifest.stats.model_dump(mode="json", exclude_none=True),
        "budget": {"usd": str(stack.budget_usd), "concurrency": CONCURRENCY},
        "seed": manifest.seed,
        "output_dir": "store",
    }
    path = project_dir / "hecklr.yaml"
    path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    return path


def _copy_tree(source: Path, target: Path) -> None:
    for path in source.rglob("*"):
        if path.is_file():
            destination = target / path.relative_to(source)
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(path.read_bytes())


def placeholder_values(stack: StackSpec, manifest: Manifest, paths: BenchPaths) -> dict[str, str]:
    """What ``{...}`` stands for in this run of ``stack``."""
    values = {
        "python": sys.executable,
        "repo": str(paths.repo),
        "seed": str(manifest.seed),
        "agent_budget_usd": str(stack.agent_budget_usd),
    }
    values.update({name: str(free_port()) for name in PORTS})
    return values


@contextmanager
def patched_environ(env: Mapping[str, str]) -> Iterator[None]:
    """``env`` set in this process for the block, the previous values restored after."""
    saved = {name: os.environ.get(name) for name in env}
    os.environ.update(env)
    try:
        yield
    finally:
        for name, value in saved.items():
            if value is None:
                os.environ.pop(name, None)
            else:
                os.environ[name] = value


@dataclass
class _SpendTracker:
    """Attributes the agent's spend to the conversation that caused it."""

    url: str | None
    last: Decimal = Decimal(0)
    first: Decimal = Decimal(0)
    per_trial: dict[str, Decimal] = field(default_factory=dict)

    def start(self) -> None:
        if self.url is not None:
            self.first = self.last = read_status(self.url).spent_usd

    def after(self, trial: Trial) -> Decimal:
        if self.url is None:
            return Decimal(0)
        now = read_status(self.url).spent_usd
        self.per_trial[trial.id], self.last = now - self.last, now
        return self.per_trial[trial.id]

    def finish(self) -> AgentStatus:
        """The agent's final status, its spend counted from the start of the run.

        Raises:
            StackError: the agent's provider failed during the run (an outage or an exhausted
                quota): its agent errors would pass for the stack's own failures.
        """
        if self.url is None:
            return AgentStatus(spent_usd=Decimal(0), failed_turns={})
        status = read_status(self.url)
        if status.outages:
            raise StackError(
                f"the agent's provider failed during the run ({status.outages}): it measured "
                "an outage or an exhausted quota, not the stack; see the agent log"
            )
        return AgentStatus(
            spent_usd=status.spent_usd - self.first, failed_turns=status.failed_turns
        )


QUOTA_ERRORS: Final = ("429", "insufficient_quota", "credit")
"""Provider errors that make a harness error an outage of Hecklr's own models."""


def run_stack(
    manifest: Manifest,
    stack: StackSpec,
    paths: BenchPaths,
    *,
    llm_providers: Mapping[str, LLMProvider] | None = None,
    on_trial: TrialReport | None = None,
) -> StackResult:
    """Start ``stack``'s agent, run its track at concurrency 1 and measure it.

    Raises:
        StackError: a required variable is unset, the agent did not start or report its spend,
            or the run did not complete with analyzable trials.
        hecklr_core.ConfigError, and the other configuration errors of ``prepare_run``.
    """
    missing = [name for name in stack.requires_env if not os.environ.get(name)]
    if missing:
        raise StackError(f"stack {stack.id} needs {', '.join(missing)} set in the environment")
    values = placeholder_values(stack, manifest, paths)
    work = paths.work_dir / stack.id / datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
    config_path = write_project(
        manifest, stack, suite_dir=paths.suite_dir, project_dir=work / "project", values=values
    )
    env = {name: fill(value, values) for name, value in stack.env.items()}
    command = [fill(part, values) for part in stack.command]
    ready = stack.ready.model_copy(
        update={
            "http": fill(stack.ready.http, values) if stack.ready.http else None,
            "line": fill(stack.ready.line, values) if stack.ready.line else None,
        }
    )
    started, load_before = time.monotonic(), os.getloadavg()[0]
    with (
        patched_environ(env),
        agent_process(command, env=env, ready=ready, cwd=paths.repo, log_path=work / "agent.log"),
    ):
        spend = _SpendTracker(fill(stack.spend_url, values) if stack.spend_url else None)
        spend.start()
        project, results = _execute(config_path, stack, llm_providers, spend, on_trial)
        agent_usd = spend.finish().spent_usd
    measured = Measured(
        results,
        spend.per_trial,
        agent_usd,
        time.monotonic() - started,
        (round(load_before, 2), round(os.getloadavg()[0], 2)),
    )
    return _result(manifest, stack, paths, project, measured)


def _execute(
    config_path: Path,
    stack: StackSpec,
    llm_providers: Mapping[str, LLMProvider] | None,
    spend: _SpendTracker,
    on_trial: TrialReport | None,
) -> tuple[Project, RunResults]:
    prepared = prepare_run(
        RunRequest(
            config_path=config_path,
            suite=stack.track,
            adaptive=False,
            agent_version=stack.id,
        )
    )
    anyio.run(
        partial(
            check_model_providers,
            prepared.project.config.models,
            prepared.models_called,
            llm_providers,
        )
    )

    def report(trial: Trial, grades: Sequence[Grade]) -> None:
        agent_usd = spend.after(trial)
        if on_trial is not None:
            on_trial(trial, agent_usd)

    results = anyio.run(
        partial(execute_run, prepared, llm_providers=llm_providers, on_trial=report)
    )
    if results.run.status is not RunStatus.COMPLETED:
        raise StackError(f"run {results.run.id} ended {results.run.status.value}, not completed")
    if all(trial.status == "harness_error" for trial in results.trials):
        first = results.trials[0].error if results.trials else "no trial ran"
        raise StackError(f"every trial of run {results.run.id} failed to run: {first}")
    quota = [t.id for t in results.trials if any(e in (t.error or "") for e in QUOTA_ERRORS)]
    if quota:
        raise StackError(
            f"{len(quota)} trials of run {results.run.id} hit a model provider's rate or credit "
            "limit; the run measured an outage, not the stack"
        )
    return prepared.project, results


@dataclass(frozen=True, slots=True)
class Measured:
    """What one run of a stack produced."""

    results: RunResults
    agent_spend: Mapping[str, Decimal]
    """The agent's spend per trial id."""
    agent_usd: Decimal
    duration_s: float
    load_average: tuple[float, float]
    """1-minute load average at the start and the end."""


def _result(
    manifest: Manifest, stack: StackSpec, paths: BenchPaths, project: Project, measured: Measured
) -> StackResult:
    results = measured.results
    analyzed = analyze_results(project, results, None, stats=manifest.stats)
    track = manifest.tracks[stack.track]
    models = manifest.models.model_dump()
    if track.asr is not None:
        models["asr"] = track.asr
    return StackResult(
        benchmark_version=manifest.version,
        suite_digest=suite_digest(paths.bench_dir, manifest),
        stack=stack,
        run_id=results.run.id,
        created_at=results.run.created_at,
        seed=manifest.seed,
        trials_per_scenario=track.trials,
        scenarios=len(analyzed.definitions.scenarios),
        models=models,
        environment=environment(paths.repo, measured.load_average),
        counts=analyzed.analysis.counts,
        metrics=stack_metrics(
            analyzed.analysis,
            results,
            analyzed.definitions.scenarios,
            measured.agent_spend,
            resamples=manifest.stats.bootstrap_resamples,
        ),
        spend=Spend(hecklr_usd=results.run.spent_usd, agent_usd=measured.agent_usd),
        duration_s=round(measured.duration_s, 1),
    )


def environment(repo: Path, load_average: tuple[float, float]) -> Environment:
    """This machine, interpreter and library versions."""
    packages = {}
    for name in PACKAGES:
        try:
            packages[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            packages[name] = "not installed"
    return Environment(
        hecklr=__version__,
        python=platform.python_version(),
        platform=platform.platform(),
        machine=platform.machine(),
        cpus=os.cpu_count() or 1,
        git_sha=detect_git_sha(repo),
        packages=packages,
        load_average=load_average,
    )
