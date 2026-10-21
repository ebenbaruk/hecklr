"""Run results on disk, one directory per run.

``<runs_dir>/<run_id>/`` holds ``results.duckdb``, Parquet exports and ``run.json``.

The DuckDB tables are the append-only source of truth. Every row keeps the record's full JSON
(``record``), so loading returns exactly the model that was written; the other columns are
denormalized copies for SQL queries. Records whose status changes (``Run``) are re-appended and
the newest wins (``runs_latest`` view). ``run.json`` (the newest ``Run``) and the Parquet files
are exports, rewritten as the run progresses, so other processes can list and read runs without
opening a database another process is writing.

All database work runs in a worker thread, one operation at a time, so writes never block the
event loop and a run's database has a single writer.
"""

import json
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from types import TracebackType
from typing import Final, Self, TypeVar

import anyio
import anyio.to_thread
import duckdb

from hecklr_schemas import Event, Grade, Run, RunId, RunStatus, Trial

RESULTS_DB: Final = "results.duckdb"
RUN_JSON: Final = "run.json"

_T = TypeVar("_T")

# Column name -> DuckDB type, in insert order. ``record`` is the full JSON of the model.
_COLUMNS: Final[Mapping[str, Mapping[str, str]]] = {
    "runs": {
        "id": "VARCHAR",
        "status": "VARCHAR",
        "suite": "VARCHAR",
        "git_sha": "VARCHAR",
        "created_at": "VARCHAR",
        "config_hash": "VARCHAR",
        "spent_usd": "DECIMAL(20,10)",
        "record": "VARCHAR",
    },
    "trials": {
        "id": "VARCHAR",
        "run_id": "VARCHAR",
        "scenario_id": "VARCHAR",
        "persona_id": "VARCHAR",
        "channel": "VARCHAR",
        "trial_index": "INTEGER",
        "seed": "HUGEINT",
        "status": "VARCHAR",
        "end_reason": "VARCHAR",
        "attempts": "INTEGER",
        "duration_ms": "BIGINT",
        "cost_usd": "DECIMAL(20,10)",
        "record": "VARCHAR",
    },
    "events": {
        "id": "VARCHAR",
        "run_id": "VARCHAR",
        "trial_id": "VARCHAR",
        "seq": "INTEGER",
        "t_ms": "BIGINT",
        "actor": "VARCHAR",
        "kind": "VARCHAR",
        "record": "VARCHAR",
    },
    "grades": {
        "id": "VARCHAR",
        "run_id": "VARCHAR",
        "trial_id": "VARCHAR",
        "grader": "VARCHAR",
        "grader_version": "VARCHAR",
        "criterion_index": "INTEGER",
        "status": "VARCHAR",
        "passed": "BOOLEAN",
        "value": "DOUBLE",
        "cost_usd": "DECIMAL(20,10)",
        "record": "VARCHAR",
    },
}

_EXPORTED_TABLES: Final = ("trials", "events", "grades")


def _schema_sql() -> str:
    statements = ["CREATE SEQUENCE IF NOT EXISTS record_seq START 1"]
    for table, columns in _COLUMNS.items():
        body = ", ".join(f"{name} {kind}" for name, kind in columns.items())
        statements.append(
            f"CREATE TABLE IF NOT EXISTS {table} "
            f"(rec_seq BIGINT DEFAULT nextval('record_seq'), {body})"
        )
    statements.append(
        "CREATE VIEW IF NOT EXISTS runs_latest AS SELECT * FROM runs "
        "QUALIFY row_number() OVER (PARTITION BY id ORDER BY rec_seq DESC) = 1"
    )
    return ";\n".join(statements)


def _insert_sql(table: str) -> str:
    # One JSON parameter per batch: binding parameters row by row costs milliseconds per row in
    # DuckDB's Python client, while unnesting one JSON array is fast. The strict variant raises on
    # a value that does not fit its column instead of silently storing NULL.
    columns = _COLUMNS[table]
    shape = json.dumps([dict(columns)], separators=(",", ":"))
    return (
        f"INSERT INTO {table} ({', '.join(columns)}) "
        f"SELECT unnest(from_json_strict(?::JSON, '{shape}'), recursive := true)"
    )


_SCHEMA_SQL: Final = _schema_sql()
_INSERT_SQL: Final = {table: _insert_sql(table) for table in _COLUMNS}


def _run_row(run: Run) -> dict[str, object]:
    return {
        "id": run.id,
        "status": run.status.value,
        "suite": run.suite,
        "git_sha": run.git_sha,
        "created_at": run.created_at.isoformat(),
        "config_hash": run.config_hash,
        "spent_usd": str(run.spent_usd),
        "record": run.model_dump_json(),
    }


def _trial_row(trial: Trial) -> dict[str, object]:
    return {
        "id": trial.id,
        "run_id": trial.run_id,
        "scenario_id": trial.scenario_id,
        "persona_id": trial.persona_id,
        "channel": trial.channel.value,
        "trial_index": trial.index,
        "seed": trial.seed,
        "status": trial.status,
        "end_reason": trial.end_reason.value if trial.end_reason is not None else None,
        "attempts": trial.attempts,
        "duration_ms": trial.duration_ms,
        "cost_usd": str(trial.cost_usd),
        "record": trial.model_dump_json(),
    }


def _event_row(run_id: RunId, event: Event) -> dict[str, object]:
    return {
        "id": event.id,
        "run_id": run_id,
        "trial_id": event.trial_id,
        "seq": event.seq,
        "t_ms": event.t_ms,
        "actor": event.actor,
        "kind": event.kind,
        "record": event.model_dump_json(),
    }


def _grade_row(run_id: RunId, grade: Grade) -> dict[str, object]:
    return {
        "id": grade.id,
        "run_id": run_id,
        "trial_id": grade.trial_id,
        "grader": grade.grader,
        "grader_version": grade.grader_version,
        "criterion_index": grade.criterion_index,
        "status": grade.status,
        "passed": grade.passed,
        "value": float(grade.value),
        "cost_usd": str(grade.cost_usd),
        "record": grade.model_dump_json(),
    }


@dataclass(frozen=True, slots=True)
class RunResults:
    """Everything stored for one run, as typed models in write order."""

    run: Run
    """The newest record of the run."""
    trials: list[Trial]
    events: list[Event]
    """All events of all trials, ordered by trial then ``seq``."""
    grades: list[Grade]

    def events_of(self, trial: Trial) -> list[Event]:
        """The events of one trial, in ``seq`` order."""
        return [event for event in self.events if event.trial_id == trial.id]

    def grades_of(self, trial: Trial) -> list[Grade]:
        """The grades of one trial, in write order."""
        return [grade for grade in self.grades if grade.trial_id == trial.id]


class RunNotFoundError(LookupError):
    """No stored run has the requested id."""


class ResultStore:
    """Reads and appends run results under ``runs_dir`` (``.hecklr/runs``)."""

    def __init__(self, runs_dir: Path) -> None:
        self.runs_dir = runs_dir
        self._connections: dict[str, duckdb.DuckDBPyConnection] = {}
        self._worker = anyio.CapacityLimiter(1)

    def run_dir(self, run_id: str) -> Path:
        """Directory of one run's artifacts."""
        return self.runs_dir / run_id

    # --- writes -------------------------------------------------------------------------------

    async def write_run(self, run: Run) -> None:
        """Append a record of ``run`` (the newest record wins) and refresh ``run.json``."""
        await self._in_worker(self._write_run, run)

    async def append_trial(self, trial: Trial) -> None:
        """Append one trial."""
        await self.append_trial_result(trial, (), ())

    async def append_events(self, run_id: RunId, events: Sequence[Event]) -> None:
        """Append events of trials of ``run_id``."""
        await self._in_worker(self._append, run_id, (), events, ())

    async def append_grades(self, run_id: RunId, grades: Sequence[Grade]) -> None:
        """Append grades of trials of ``run_id``."""
        await self._in_worker(self._append, run_id, (), (), grades)

    async def append_trial_result(
        self, trial: Trial, events: Sequence[Event], grades: Sequence[Grade]
    ) -> None:
        """Append a finished trial with its events and grades in one transaction."""
        await self._in_worker(self._append, trial.run_id, (trial,), events, grades)

    async def export(self, run_id: RunId) -> list[Path]:
        """Write ``trials``, ``events`` and ``grades`` as Parquet into the run directory."""
        return await self._in_worker(self._export, run_id)

    # --- reads --------------------------------------------------------------------------------

    async def load_run(self, run_id: str) -> RunResults:
        """Every record of one run as typed models.

        Raises:
            RunNotFoundError: no run with this id is stored.
        """
        return await self._in_worker(self._load, run_id)

    def list_runs(self) -> list[Run]:
        """Every stored run (its newest record), oldest first, read from ``run.json`` files."""
        runs = [
            Run.model_validate_json(path.read_text(encoding="utf-8"))
            for path in self.runs_dir.glob(f"*/{RUN_JSON}")
        ]
        return sorted(runs, key=lambda run: (run.created_at, run.id))

    def latest_run(
        self,
        *,
        suite: str | None = None,
        git_sha: str | None = None,
        statuses: Iterable[RunStatus] = (RunStatus.COMPLETED,),
    ) -> Run | None:
        """The newest run matching every given filter, e.g. the baseline for a suite on a commit."""
        wanted = set(statuses)
        matching = [
            run
            for run in self.list_runs()
            if run.status in wanted
            and (suite is None or run.suite == suite)
            and (git_sha is None or run.git_sha == git_sha)
        ]
        return matching[-1] if matching else None

    # --- lifecycle ----------------------------------------------------------------------------

    def close(self) -> None:
        """Close every open database."""
        for connection in self._connections.values():
            connection.close()
        self._connections.clear()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self._in_worker(self.close)

    # --- worker-thread side -------------------------------------------------------------------

    async def _in_worker(self, func: Callable[..., _T], *args: object) -> _T:
        # Not abandoned on cancel: a write that started always completes, so a cancelled run
        # never leaves a half-written transaction or a connection in use by a stray thread.
        return await anyio.to_thread.run_sync(partial(func, *args), limiter=self._worker)

    def _connection(self, run_id: str, *, create: bool) -> duckdb.DuckDBPyConnection:
        connection = self._connections.get(run_id)
        if connection is not None:
            return connection
        path = self.run_dir(run_id) / RESULTS_DB
        if not create and not path.exists():
            raise RunNotFoundError(f"no stored run {run_id!r} under {self.runs_dir}")
        path.parent.mkdir(parents=True, exist_ok=True)
        connection = duckdb.connect(str(path))
        connection.execute(_SCHEMA_SQL)
        self._connections[run_id] = connection
        return connection

    def _write_run(self, run: Run) -> None:
        connection = self._connection(run.id, create=True)
        connection.execute(_INSERT_SQL["runs"], [json.dumps([_run_row(run)])])
        target = self.run_dir(run.id) / RUN_JSON
        temporary = target.with_suffix(".json.tmp")
        temporary.write_text(run.model_dump_json(indent=2), encoding="utf-8")
        temporary.replace(target)

    def _append(
        self,
        run_id: RunId,
        trials: Sequence[Trial],
        events: Sequence[Event],
        grades: Sequence[Grade],
    ) -> None:
        batches = {
            "trials": [_trial_row(trial) for trial in trials],
            "events": [_event_row(run_id, event) for event in events],
            "grades": [_grade_row(run_id, grade) for grade in grades],
        }
        connection = self._connection(run_id, create=True)
        connection.execute("BEGIN TRANSACTION")
        try:
            for table, rows in batches.items():
                if rows:
                    connection.execute(_INSERT_SQL[table], [json.dumps(rows)])
        except BaseException:
            connection.execute("ROLLBACK")
            raise
        connection.execute("COMMIT")

    def _load(self, run_id: str) -> RunResults:
        connection = self._connection(run_id, create=False)
        run_rows = connection.execute(
            "SELECT record FROM runs_latest WHERE id = ?", [run_id]
        ).fetchall()
        if not run_rows:
            raise RunNotFoundError(f"run {run_id!r} has no run record")

        def records(sql: str) -> list[str]:
            return [row[0] for row in connection.execute(sql, [run_id]).fetchall()]

        return RunResults(
            run=Run.model_validate_json(run_rows[0][0]),
            trials=[
                Trial.model_validate_json(record)
                for record in records("SELECT record FROM trials WHERE run_id = ? ORDER BY rec_seq")
            ],
            events=[
                Event.model_validate_json(record)
                for record in records(
                    "SELECT record FROM events WHERE run_id = ? ORDER BY trial_id, seq, rec_seq"
                )
            ],
            grades=[
                Grade.model_validate_json(record)
                for record in records("SELECT record FROM grades WHERE run_id = ? ORDER BY rec_seq")
            ],
        )

    def _export(self, run_id: RunId) -> list[Path]:
        connection = self._connection(run_id, create=False)
        written = []
        for table in _EXPORTED_TABLES:
            path = self.run_dir(run_id) / f"{table}.parquet"
            literal = str(path).replace("'", "''")
            # COPY takes no bound parameters; the table is a constant and the path is quoted.
            sql = f"COPY (SELECT * FROM {table} ORDER BY rec_seq) TO '{literal}' (FORMAT parquet)"  # noqa: S608
            connection.execute(sql)
            written.append(path)
        return written
