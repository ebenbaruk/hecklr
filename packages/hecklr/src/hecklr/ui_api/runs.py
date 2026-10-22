"""Stored runs for the UI: listed, loaded and analyzed without ever locking a run (ADR 0016a).

A run's DuckDB has a single writer (ADR 0001a), so readers use ``run.json`` and the Parquet
exports written when the run ends. A run without exports (killed before its end) is read from
``results.duckdb`` opened read-only; one still held by its writer is reported as busy.
"""

import threading
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Final

import duckdb

from hecklr.results import analyze_results
from hecklr_core import RUNS_DIR, Project, RunResults
from hecklr_core.storage import RESULTS_DB, RUN_JSON
from hecklr_report import Analysis
from hecklr_schemas import Event, Grade, Run, Trial, is_ulid

_ORDER: Final = {
    "trials": "rec_seq",
    "events": "trial_id, seq, rec_seq",
    "grades": "rec_seq",
}
"""The orders ``ResultStore.load_run`` returns, so both readers give identical results."""

ANALYSIS_CACHE_SIZE: Final = 32


class UnknownRunError(LookupError):
    """No stored run has this id."""


class RunBusyError(RuntimeError):
    """The run's results cannot be read yet: its writer still holds the database."""


@dataclass(frozen=True, slots=True)
class StoredRun:
    """One run directory and the newest ``Run`` record its ``run.json`` holds."""

    run: Run
    directory: Path
    version: int
    """``run.json`` modification time in ns: changes whenever the run record is rewritten."""


class RunLibrary:
    """The project's stored runs, read-only, with loaded results and analyses cached.

    Thread-safe: FastAPI runs the synchronous endpoints in a thread pool.
    """

    def __init__(self, project: Project) -> None:
        self.project = project
        self.runs_dir = project.output_dir / RUNS_DIR
        self._lock = threading.Lock()
        self._results: OrderedDict[tuple[str, int], RunResults] = OrderedDict()
        self._analyses: OrderedDict[tuple[str, int, str | None, int | None], Analysis] = (
            OrderedDict()
        )

    def list_runs(self) -> list[StoredRun]:
        """Every stored run, newest first."""
        stored = [_stored(path.parent) for path in self.runs_dir.glob(f"*/{RUN_JSON}")]
        return sorted(stored, key=lambda s: (s.run.created_at, s.run.id), reverse=True)

    def get(self, run_id: str) -> StoredRun:
        """The stored run ``run_id``.

        Raises:
            UnknownRunError: the id is malformed or names no stored run.
        """
        directory = self.runs_dir / run_id
        if not is_ulid(run_id) or not (directory / RUN_JSON).is_file():
            raise UnknownRunError(f"no stored run {run_id!r}")
        return _stored(directory)

    def results(self, run_id: str) -> RunResults:
        """Every record of one run.

        Raises:
            UnknownRunError: no such run.
            RunBusyError: the run is still being written.
        """
        stored = self.get(run_id)
        key = (run_id, stored.version)
        return self._cached(self._results, key, lambda: _load(stored))

    def analysis(self, run_id: str, baseline_id: str | None = None) -> Analysis:
        """The run's analysis, compared with ``baseline_id`` when given (as ``hecklr report``)."""
        candidate = self.get(run_id)
        baseline = self.get(baseline_id) if baseline_id is not None else None
        key = (
            run_id,
            candidate.version,
            baseline_id,
            baseline.version if baseline is not None else None,
        )
        return self._cached(self._analyses, key, lambda: self._analyze(run_id, baseline_id))

    def _analyze(self, run_id: str, baseline_id: str | None) -> Analysis:
        base = self.results(baseline_id) if baseline_id is not None else None
        return analyze_results(self.project, self.results(run_id), base).analysis

    def _cached[K, V](self, cache: OrderedDict[K, V], key: K, compute: Callable[[], V]) -> V:
        with self._lock:
            if key in cache:
                cache.move_to_end(key)
                return cache[key]
        # Computed outside the lock: an analysis takes seconds and other requests must not wait.
        value = compute()
        with self._lock:
            cache[key] = value
            while len(cache) > ANALYSIS_CACHE_SIZE:
                cache.popitem(last=False)
        return value


def _stored(directory: Path) -> StoredRun:
    path = directory / RUN_JSON
    return StoredRun(
        run=Run.model_validate_json(path.read_text(encoding="utf-8")),
        directory=directory,
        version=path.stat().st_mtime_ns,
    )


def _load(stored: StoredRun) -> RunResults:
    exports = {table: stored.directory / f"{table}.parquet" for table in _ORDER}
    if all(path.is_file() for path in exports.values()):
        with duckdb.connect() as connection:
            records = {
                table: _records(
                    connection,
                    f"SELECT record FROM read_parquet(?) ORDER BY {order}",  # noqa: S608 - constant
                    str(path),
                )
                for (table, order), path in zip(_ORDER.items(), exports.values(), strict=True)
            }
    else:
        records = _read_database(stored)
    return RunResults(
        run=stored.run,
        trials=[Trial.model_validate_json(r) for r in records["trials"]],
        events=[Event.model_validate_json(r) for r in records["events"]],
        grades=[Grade.model_validate_json(r) for r in records["grades"]],
    )


def _read_database(stored: StoredRun) -> dict[str, list[str]]:
    database = stored.directory / RESULTS_DB
    if not database.is_file():
        raise RunBusyError(f"run {stored.run.id} has no results yet ({stored.run.status.value})")
    try:
        connection = duckdb.connect(str(database), read_only=True)
    except (duckdb.IOException, duckdb.ConnectionException) as exc:
        # IOException: another process holds the write lock. ConnectionException: this process
        # (an embedding caller) has it open for writing.
        raise RunBusyError(
            f"run {stored.run.id} is still being written ({stored.run.status.value}); "
            "its results open when it ends"
        ) from exc
    with connection:
        return {
            table: _records(
                connection,
                # Table and ORDER BY come from the constant above; the run id is bound.
                f"SELECT record FROM {table} WHERE run_id = ? ORDER BY {order}",  # noqa: S608
                stored.run.id,
            )
            for table, order in _ORDER.items()
        }


def _records(connection: duckdb.DuckDBPyConnection, sql: str, parameter: str) -> list[str]:
    return [str(row[0]) for row in connection.execute(sql, [parameter]).fetchall()]
