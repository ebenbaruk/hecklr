"""ResultStore: exact round trips, newest-record-wins runs, exports, listing and concurrency."""

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import anyio
import anyio.lowlevel
import duckdb
import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st
from pydantic import JsonValue

from hecklr_core import ResultStore, RunNotFoundError
from hecklr_core.storage import RESULTS_DB, RUN_JSON
from hecklr_schemas import (
    AgentRef,
    AudioSpanPayload,
    Budget,
    Channel,
    EmailAttachment,
    EmailMessage,
    EmailPayload,
    EndReason,
    ErrorPayload,
    Event,
    EventId,
    EventPayload,
    EventRef,
    FaultKind,
    Grade,
    GradeId,
    HandoffPayload,
    InterruptPayload,
    JudgeVote,
    Run,
    RunCounts,
    RunId,
    RunStatus,
    SilencePayload,
    ToolCallPayload,
    ToolResultPayload,
    Trial,
    TrialId,
    TrialSpec,
    UtterancePayload,
    WorldSnapshot,
    new_id,
)

T0 = datetime(2026, 10, 21, 9, 30, 15, 123456, tzinfo=UTC)


def _run(**fields: object) -> Run:
    base: dict[str, object] = {
        "id": RunId(new_id()),
        "created_at": T0,
        "git_sha": "abc123",
        "agent": AgentRef(name="acme", version="v1", adapters={Channel.CHAT: "http"}),
        "config_hash": "f" * 64,
        "seed": 2**70,
        "suite": "ci",
        "matrix": [
            TrialSpec(scenario_id="s", persona_id="p", channel=Channel.CHAT, index=0, seed=2**62)
        ],
        "budget": Budget(usd=Decimal("25.5"), concurrency=8),
        "status": RunStatus.RUNNING,
        "counts": RunCounts(planned=1),
        "spent_usd": Decimal("0.000000123456789"),
        "simulator_model": "fake/sim",
        "judge_model": "fake/judge",
    }
    return Run.model_validate({**base, **fields})


def _trial(run_id: RunId, **fields: object) -> Trial:
    snapshot = WorldSnapshot(
        world_id="acme",
        taken_at_ms=0,
        collections={"appointments": {"C-1": {"slot": "2026-10-23T09:00", "n": [1, 2.5, None]}}},
    )
    base: dict[str, object] = {
        "id": TrialId(new_id()),
        "run_id": run_id,
        "scenario_id": "s",
        "persona_id": "p",
        "channel": Channel.CHAT,
        "index": 0,
        "seed": 2**62,
        "status": "ok",
        "end_reason": EndReason.GOAL_ACHIEVED,
        "started_at": T0,
        "duration_ms": 1234,
        "cost_usd": Decimal("0.0123456789012"),
        "world_before": snapshot,
        "world_after": snapshot.model_copy(update={"taken_at_ms": 1200}),
        "turns": 6,
        "contradictions": 1,
        "attempts": 2,
        "error": None,
    }
    return Trial.model_validate({**base, **fields})


def _payloads() -> list[EventPayload]:
    email = EmailMessage(
        message_id="<1@acme.test>",
        subject="Visite",
        body="Bonjour, déplacer la visite ? 🙏",
        from_addr="camille@customers.hecklr.test",
        to_addrs=["support@acme.test"],
        references=["<0@acme.test>"],
        attachments=[EmailAttachment(filename="a.bin", data=b"\x00\xff\x10")],
    )
    return [
        UtterancePayload(text='quote " and \\ and \n', turn=0, end_ms=10, latency_ms=3),
        AudioSpanPayload(start_ms=5, end_ms=9, start_sample=80, sample_rate=16000),
        ToolCallPayload(call_id="c", tool="t", args={"a": {"b": [1, "x", None, True]}}),
        ToolResultPayload(call_id="c", tool="t", error="boom", latency_ms=7, fault=FaultKind.SLOW),
        InterruptPayload(target="agent", reaction_ms=120),
        SilencePayload(start_ms=1, end_ms=4000, attributed_to="agent"),
        EmailPayload(message=email),
        HandoffPayload(reason="gas smell", target="on-call"),
        ErrorPayload(source="harness", message="x", retryable=True),
    ]


def _events(trial_id: TrialId) -> list[Event]:
    return [
        Event(
            id=EventId(new_id()), trial_id=trial_id, seq=seq, t_ms=5 + seq, actor="agent", payload=p
        )
        for seq, p in enumerate(_payloads())
    ]


def _grade(trial_id: TrialId, event: Event, **fields: object) -> Grade:
    base: dict[str, object] = {
        "id": GradeId(new_id()),
        "trial_id": trial_id,
        "grader": "judge:resolved",
        "grader_version": "2",
        "criterion_index": 1,
        "value": 0.75,
        "passed": True,
        "rationale": "résolu",
        "evidence": [EventRef(trial_id=trial_id, event_id=event.id)],
        "judge_model": "fake/judge",
        "votes": [JudgeVote(passed=True, rationale="r", model="fake/judge")],
        "cost_usd": Decimal("0.0000001"),
    }
    return Grade.model_validate({**base, **fields})


@pytest.fixture
async def store(tmp_path: Path) -> ResultStore:
    return ResultStore(tmp_path / "runs")


@pytest.mark.anyio
async def test_every_model_round_trips_exactly(tmp_path: Path) -> None:
    run = _run()
    trial = _trial(run.id)
    events = _events(trial.id)
    grades = [
        _grade(trial.id, events[0]),
        _grade(trial.id, events[0], value=True, status="ok", votes=[], criterion_index=None),
        _grade(trial.id, events[0], status="skipped", passed=False, value=False),
    ]
    async with ResultStore(tmp_path / "runs") as store:
        await store.write_run(run)
        await store.append_trial_result(trial, events, grades)
        loaded = await store.load_run(run.id)
    # A fresh store reads the same thing from disk.
    async with ResultStore(tmp_path / "runs") as reopened:
        from_disk = await reopened.load_run(run.id)
    for results in (loaded, from_disk):
        assert results.run == run
        assert results.trials == [trial]
        assert results.events == events
        assert results.grades == grades
        assert results.events_of(trial) == events
        assert results.grades_of(trial) == grades
        assert results.run.model_dump_json() == run.model_dump_json()


@pytest.mark.anyio
async def test_individual_appends(store: ResultStore) -> None:
    run = _run()
    trial = _trial(run.id)
    events = _events(trial.id)
    await store.write_run(run)
    await store.append_trial(trial)
    await store.append_events(run.id, events[:3])
    await store.append_events(run.id, events[3:])
    await store.append_grades(run.id, [_grade(trial.id, events[0])])
    loaded = await store.load_run(run.id)
    store.close()
    assert loaded.trials == [trial]
    assert loaded.events == events
    assert len(loaded.grades) == 1


json_values = st.recursive(
    st.none() | st.booleans() | st.integers(-(2**53), 2**53) | st.text(),
    lambda inner: st.lists(inner, max_size=3) | st.dictionaries(st.text(), inner, max_size=3),
    max_leaves=10,
)


@settings(
    max_examples=40, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture]
)
@given(text=st.text(), args=st.dictionaries(st.text(), json_values, max_size=4))
def test_arbitrary_text_and_json_round_trip(
    tmp_path: Path, text: str, args: dict[str, JsonValue]
) -> None:
    async def scenario() -> None:
        run = _run()
        trial_id = TrialId(new_id())
        events = [
            Event(
                id=EventId(new_id()),
                trial_id=trial_id,
                seq=0,
                t_ms=0,
                actor="user",
                payload=UtterancePayload(text=text, turn=0),
            ),
            Event(
                id=EventId(new_id()),
                trial_id=trial_id,
                seq=1,
                t_ms=0,
                actor="agent",
                payload=ToolCallPayload(call_id="c", tool="t", args=args),
            ),
        ]
        async with ResultStore(tmp_path / new_id()) as store:
            await store.write_run(run)
            await store.append_events(run.id, events)
            assert (await store.load_run(run.id)).events == events

    anyio.run(scenario)


@pytest.mark.anyio
async def test_newest_run_record_wins_and_history_is_kept(store: ResultStore) -> None:
    run = _run()
    await store.write_run(run)
    finished = run.model_copy(
        update={"status": RunStatus.COMPLETED, "counts": RunCounts(planned=1, ok=1)}
    )
    await store.write_run(finished)
    assert (await store.load_run(run.id)).run == finished
    assert store.list_runs() == [finished]
    store.close()
    with duckdb.connect(str(store.run_dir(run.id) / RESULTS_DB), read_only=True) as db:
        statuses = db.execute("SELECT status FROM runs ORDER BY rec_seq").fetchall()
    assert statuses == [("running",), ("completed",)]


@pytest.mark.anyio
async def test_list_and_latest_runs(store: ResultStore) -> None:
    old = _run(created_at=T0, git_sha="a")
    newer = _run(created_at=T0 + timedelta(hours=1), git_sha="b", status=RunStatus.COMPLETED)
    newest = _run(created_at=T0 + timedelta(hours=2), git_sha="a", status=RunStatus.COMPLETED)
    other_suite = _run(
        created_at=T0 + timedelta(hours=3), suite="nightly", status=RunStatus.COMPLETED
    )
    failed = _run(created_at=T0 + timedelta(hours=4), status=RunStatus.FAILED)
    for run in (newest, old, other_suite, failed, newer):
        await store.write_run(run)
    assert [run.id for run in store.list_runs()] == [
        old.id,
        newer.id,
        newest.id,
        other_suite.id,
        failed.id,
    ]
    assert store.latest_run() == other_suite
    assert store.latest_run(suite="ci") == newest
    assert store.latest_run(suite="ci", git_sha="b") == newer
    assert store.latest_run(suite="ci", statuses=[RunStatus.FAILED]) == failed
    assert store.latest_run(suite="ci", git_sha="zzz") is None
    store.close()


def test_list_runs_of_an_empty_directory(tmp_path: Path) -> None:
    assert ResultStore(tmp_path / "missing").list_runs() == []


@pytest.mark.anyio
async def test_parquet_exports(store: ResultStore) -> None:
    run = _run()
    trial = _trial(run.id)
    events = _events(trial.id)
    await store.write_run(run)
    await store.append_trial_result(trial, events, [_grade(trial.id, events[0])])
    paths = await store.export(run.id)
    store.close()
    assert [path.name for path in paths] == ["trials.parquet", "events.parquet", "grades.parquet"]
    db = duckdb.connect()
    (count, kind) = db.execute(
        f"SELECT count(*), min(kind) FROM read_parquet('{paths[1]}')"  # noqa: S608 -- test path
    ).fetchone() or (0, None)
    assert (count, kind) == (len(events), "audio_span")
    record = db.execute(f"SELECT record FROM read_parquet('{paths[0]}')").fetchone()  # noqa: S608
    assert record is not None
    assert Trial.model_validate_json(record[0]) == trial
    run_json = store.run_dir(run.id) / RUN_JSON
    assert Run.model_validate_json(run_json.read_text()) == run


@pytest.mark.anyio
async def test_unknown_run_is_reported(store: ResultStore) -> None:
    with pytest.raises(RunNotFoundError, match="no stored run"):
        await store.load_run(new_id())
    with pytest.raises(RunNotFoundError, match="no stored run"):
        await store.export(RunId(new_id()))


@pytest.mark.anyio
async def test_run_without_a_run_record_is_reported(store: ResultStore) -> None:
    run_id = RunId(new_id())
    await store.append_events(run_id, _events(TrialId(new_id())))
    with pytest.raises(RunNotFoundError, match="has no run record"):
        await store.load_run(run_id)
    store.close()


@pytest.mark.anyio
async def test_concurrent_appends_from_many_tasks(store: ResultStore) -> None:
    run = _run()
    await store.write_run(run)
    trials = [_trial(run.id, index=i) for i in range(64)]

    async def append(trial: Trial) -> None:
        await store.append_trial_result(trial, _events(trial.id), [])

    async with anyio.create_task_group() as group:
        for trial in trials:
            group.start_soon(append, trial)
    loaded = await store.load_run(run.id)
    store.close()
    assert sorted(t.id for t in loaded.trials) == sorted(t.id for t in trials)
    assert len(loaded.events) == 64 * len(_payloads())


@pytest.mark.anyio
async def test_failed_batch_is_rolled_back(store: ResultStore) -> None:
    run = _run()
    trial = _trial(run.id)
    await store.write_run(run)
    # A seed beyond HUGEINT cannot be stored: the whole trial result must be rejected.
    huge = trial.model_copy(update={"seed": 2**200})
    with pytest.raises(duckdb.Error):
        await store.append_trial_result(huge, _events(trial.id), [])
    loaded = await store.load_run(run.id)
    store.close()
    assert loaded.trials == []
    assert loaded.events == []


@pytest.mark.anyio
async def test_writes_do_not_block_the_event_loop(store: ResultStore) -> None:
    run = _run()
    await store.write_run(run)
    trial = _trial(run.id)
    events = [
        e.model_copy(update={"id": EventId(new_id())})
        for _ in range(300)
        for e in _events(trial.id)
    ]
    ticks = 0

    async def heartbeat() -> None:
        nonlocal ticks
        while True:
            ticks += 1
            await anyio.lowlevel.checkpoint()

    async with anyio.create_task_group() as group:
        group.start_soon(heartbeat)
        await store.append_events(run.id, events)
        group.cancel_scope.cancel()
    store.close()
    assert ticks > 1
