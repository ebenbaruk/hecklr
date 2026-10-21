"""Stored runs for ``compare``, ``gate``, ``report`` and ``calibrate``: find them, analyze them.

A run is named by its id or an alias:

- ``latest``: the newest finished run (of ``--suite`` when given), whatever its status, so a gate
  on a crashed run exits 3 instead of silently gating an older one;
- ``baseline``: relative to a candidate run, the baseline it recorded (``hecklr run --baseline``),
  else the newest *completed* run of the same suite created before it.
"""

from collections.abc import Mapping, Sequence
from contextlib import closing
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import anyio

from hecklr_core import RUNS_DIR, ConfigError, Project, ResultStore, RunResults, resolve_suite
from hecklr_report import Analysis, analyze
from hecklr_schemas import Persona, Run, RunStatus, Scenario, StatsConfig, is_ulid
from hecklr_stats import JudgeCalibration

LATEST: Final = "latest"
BASELINE: Final = "baseline"

FINISHED: Final = (
    RunStatus.COMPLETED,
    RunStatus.BUDGET_EXCEEDED,
    RunStatus.CANCELLED,
    RunStatus.FAILED,
)
"""Statuses a run can end in; ``latest`` never picks a run that is still pending or running."""


class RunLookupError(LookupError):
    """A run reference names no stored run."""


def runs_dir(project: Project) -> Path:
    """Where the project's runs are stored."""
    return project.output_dir / RUNS_DIR


def resolve_run(store: ResultStore, ref: str, *, suite: str | None = None) -> Run:
    """The stored run ``ref`` names: a run id or ``latest``.

    Raises:
        RunLookupError: no such run, or no finished run (of ``suite``) for ``latest``.
    """
    if ref == LATEST:
        run = store.latest_run(suite=suite, statuses=FINISHED)
        if run is None:
            scope = f" of suite {suite!r}" if suite else ""
            raise RunLookupError(f"no finished run{scope} under {store.runs_dir}")
        return run
    if not is_ulid(ref):
        raise RunLookupError(f"{ref!r} is neither a run id nor one of {LATEST!r}, {BASELINE!r}")
    for run in store.list_runs():
        if run.id == ref:
            return run
    raise RunLookupError(f"no stored run {ref!r} under {store.runs_dir}")


def resolve_baseline(store: ResultStore, ref: str, candidate: Run) -> Run | None:
    """The baseline of ``candidate``: a run id, or ``baseline`` (``None`` when there is none).

    Raises:
        RunLookupError: ``ref`` is an id that names no stored run, or names ``candidate`` itself.
    """
    if ref != BASELINE:
        return _distinct(resolve_run(store, ref), candidate)
    if candidate.baseline_id is not None:
        return _distinct(resolve_run(store, candidate.baseline_id), candidate)
    earlier = [
        run
        for run in store.list_runs()
        if run.status is RunStatus.COMPLETED
        and run.suite == candidate.suite
        and run.id != candidate.id
        and (run.created_at, run.id) < (candidate.created_at, candidate.id)
    ]
    return earlier[-1] if earlier else None


def _distinct(baseline: Run, candidate: Run) -> Run:
    # A run compared with itself always shows "no change". In CI that usually means the candidate
    # run never got recorded and `latest` fell back to the fetched baseline, which must not pass.
    if baseline.id == candidate.id:
        raise RunLookupError(
            f"the candidate and the baseline are the same run {candidate.id}; nothing would be "
            "compared (did the candidate run fail before it was recorded?)"
        )
    return baseline


def open_store(project: Project) -> closing[ResultStore]:
    """The project's result store, as a context manager that closes it."""
    return closing(ResultStore(runs_dir(project)))


@dataclass(frozen=True, slots=True)
class Definitions:
    """Scenario and persona definitions the analysis reads titles, tags and voice slices from."""

    scenarios: Mapping[str, Scenario]
    personas: Mapping[str, Persona]
    missing: Sequence[str] = ()
    """Suites of the analyzed runs that no longer resolve (their scenarios show by id only)."""


def definitions(project: Project, suites: Sequence[str | None]) -> Definitions:
    """The current definitions of the scenarios and personas of ``suites``."""
    scenarios: dict[str, Scenario] = {}
    personas: dict[str, Persona] = {}
    missing: list[str] = []
    for name in dict.fromkeys(s for s in suites if s is not None):
        try:
            suite = resolve_suite(project, name)
        except ConfigError:
            # A suite renamed or removed since the run: the stored trials still analyze.
            missing.append(name)
            continue
        scenarios.update(suite.scenarios)
        personas.update(suite.personas)
    return Definitions(scenarios, personas, missing)


@dataclass(frozen=True, slots=True)
class Analyzed:
    """An analysis and the stored runs behind it."""

    analysis: Analysis
    candidate: RunResults
    baseline: RunResults | None
    definitions: Definitions


def analyze_runs(
    project: Project,
    candidate: Run,
    baseline: Run | None,
    *,
    stats: StatsConfig | None = None,
    judge_calibrations: Sequence[JudgeCalibration] = (),
) -> Analyzed:
    """Load ``candidate`` (and ``baseline``) from the store and analyze them."""
    cand = load_results(project, candidate.id)
    base = load_results(project, baseline.id) if baseline is not None else None
    return analyze_results(project, cand, base, stats=stats, judge_calibrations=judge_calibrations)


def load_results(project: Project, run_id: str) -> RunResults:
    """Every stored record of one run of ``project``.

    Raises:
        hecklr_core.RunNotFoundError: no run with this id is stored.
    """
    with open_store(project) as store:
        return anyio.run(store.load_run, run_id)


def analyze_results(
    project: Project,
    candidate: RunResults,
    baseline: RunResults | None,
    *,
    stats: StatsConfig | None = None,
    judge_calibrations: Sequence[JudgeCalibration] = (),
) -> Analyzed:
    """Analyze loaded results with the project's statistics settings and definitions."""
    suites = [candidate.run.suite, baseline.run.suite if baseline is not None else None]
    defs = definitions(project, suites)
    analysis = analyze(
        candidate,
        baseline,
        stats or project.config.stats,
        defs.scenarios,
        personas=defs.personas,
        judge_calibrations=judge_calibrations,
    )
    return Analyzed(analysis, candidate, baseline, defs)
