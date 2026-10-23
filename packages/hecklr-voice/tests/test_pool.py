"""The DSP worker pool: workers that never run the parent's main file (#31) and are never
retired for idling (#47), with anyio's process-pool semantics for errors and cancellation."""

import importlib
import io
import os
import pickle
import subprocess
import sys
import threading
import time
from pathlib import Path

import anyio
import pytest

from hecklr_voice import _dsp_worker, pool
from hecklr_voice.pool import dsp_workers, run_dsp, warm_dsp_pool

pytestmark = pytest.mark.anyio


def _worker_pids() -> set[int]:
    return {worker.process.pid for worker in pool.current_pool().idle}


async def test_issue47_idle_workers_are_never_retired() -> None:
    # anyio retired a worker idle for WORKER_MAX_IDLE_TIME (300 s) on the next dispatch, last in
    # first out, so a peak of jobs after a quiet spell started fresh workers that loaded their
    # models mid-call. Hecklr's pool has no idle limit: the warmed workers survive idle gaps.
    await warm_dsp_pool()
    warmed = _worker_pids()
    assert len(warmed) == dsp_workers()

    for _ in range(3):
        await anyio.sleep(0.2)
        async with anyio.create_task_group() as group:
            for _ in range(dsp_workers()):
                group.start_soon(run_dsp, time.sleep, 0.05)

    assert _worker_pids() == warmed


PROBE = """
import sys


def loaded(name):
    return name in sys.modules
"""


async def test_warm_workers_have_the_voice_engine_imported_before_any_job(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # A cold worker's first job imports numpy, scipy and every DSP module: seconds on a loaded
    # machine, which a warmed pool must not spend in the middle of a call.
    (tmp_path / "hecklr_pool_probe.py").write_text(PROBE)
    monkeypatch.syspath_prepend(str(tmp_path))
    probe = importlib.import_module("hecklr_pool_probe")
    await warm_dsp_pool()
    results: list[bool] = []

    async def ask() -> None:
        results.append(await run_dsp(probe.loaded, "hecklr_voice.impair"))

    async with anyio.create_task_group() as group:
        for _ in range(dsp_workers()):
            group.start_soon(ask)
    assert results == [True] * dsp_workers()


async def test_warming_never_starts_more_workers_than_the_limit() -> None:
    async with anyio.create_task_group() as group:
        for _ in range(3):
            group.start_soon(warm_dsp_pool)
        group.start_soon(run_dsp, time.sleep, 0.01)
    await warm_dsp_pool()
    assert len(pool.current_pool().processes) == dsp_workers()
    assert len(_worker_pids()) == dsp_workers()


ENTRY_SCRIPT = """
import os
import sys

import anyio
import duckdb  # duckdb refuses to import in a process without a __main__ module (#31)

from hecklr_voice.pool import run_dsp

with open(sys.argv[1], "a") as runs:  # no __name__ guard: the workers must not run this file
    runs.write("ran\\n")


async def main() -> None:
    pids = {await run_dsp(os.getpid) for _ in range(2)}
    assert os.getpid() not in pids
    # A job whose own imports pull in duckdb works too: the worker keeps a __main__ module.
    await run_dsp(exec, "import duckdb")
    print("ok", duckdb.__version__)


anyio.run(main)
"""


def test_issue31_workers_start_when_the_entry_script_imports_duckdb(tmp_path: Path) -> None:
    script = tmp_path / "agent.py"
    script.write_text(ENTRY_SCRIPT)
    runs = tmp_path / "runs.txt"

    done = subprocess.run(  # noqa: S603 -- this interpreter running the test's own script
        [sys.executable, str(script), str(runs)],
        capture_output=True,
        text=True,
        timeout=120,
        check=False,
    )

    assert done.returncode == 0, done.stderr
    assert done.stdout.startswith("ok ")
    assert runs.read_text() == "ran\n", "the entry script ran once, in the parent only"


async def test_job_errors_are_raised_with_their_own_type_and_the_worker_is_kept() -> None:
    first = await run_dsp(os.getpid)
    with pytest.raises(ValueError, match="invalid literal"):
        await run_dsp(int, "not a number")
    assert await run_dsp(os.getpid) == first


async def test_an_unpicklable_result_is_an_error_not_a_broken_worker() -> None:
    first = await run_dsp(os.getpid)
    with pytest.raises(RuntimeError, match="could not be pickled"):
        await run_dsp(threading.Lock)
    assert await run_dsp(os.getpid) == first


async def test_a_worker_that_dies_mid_job_is_replaced() -> None:
    with pytest.raises(anyio.BrokenWorkerProcess):
        await run_dsp(os._exit, 3)
    assert await run_dsp(int, "7") == 7


async def test_cancelling_a_job_kills_its_worker_and_warming_replaces_it() -> None:
    await warm_dsp_pool()
    started = time.monotonic()
    with anyio.move_on_after(0.2):
        await run_dsp(time.sleep, 30)
    assert time.monotonic() - started < 5
    assert len(pool.current_pool().processes) == dsp_workers() - 1
    await warm_dsp_pool()
    assert len(pool.current_pool().processes) == dsp_workers()


async def test_an_idle_worker_that_exited_is_skipped() -> None:
    await warm_dsp_pool()
    victim = pool.current_pool().idle[-1].process
    victim.kill()
    await victim.wait()
    assert await run_dsp(int, "5") == 5
    assert victim not in pool.current_pool().processes


def _frame(value: object) -> bytes:
    payload = pickle.dumps(value)
    return b"%d\n" % len(payload) + payload


def _serve(*requests: object) -> tuple[list[tuple[bytes, object]], list[str]]:
    """Run the worker loop in-process on ``requests``: its (status, value) replies and the
    ``sys.path`` it left (restored afterwards)."""
    replies = io.BytesIO()
    saved = sys.path[:]
    try:
        _dsp_worker.serve(io.BytesIO(b"".join(_frame(request) for request in requests)), replies)
        adopted = sys.path[:]
    finally:
        sys.path[:] = saved
    stream = io.BytesIO(replies.getvalue())
    parsed = []
    while header := stream.readline():
        status, length = header.split()
        parsed.append((status, pickle.loads(stream.read(int(length)))))  # noqa: S301 -- ours
    return parsed, adopted


def test_worker_adopts_the_parent_path_then_runs_jobs_until_end_of_input() -> None:
    parent_path = [*sys.path, "/parent/only"]
    replies, adopted = _serve(parent_path, (int, ("42",)), (int, ("x",)))
    assert adopted == parent_path
    assert [status for status, _ in replies] == [b"RETURN", b"RETURN", b"EXCEPTION"]
    assert [value for _, value in replies[:2]] == [None, 42]
    assert isinstance(replies[2][1], ValueError)


def test_worker_reports_an_unpicklable_result_as_an_error() -> None:
    ((_, ready), (status, error)), _ = _serve(sys.path, (threading.Lock, ()))
    assert ready is None
    assert status == b"EXCEPTION"
    assert isinstance(error, RuntimeError)
    assert "could not be pickled" in str(error)


def test_worker_exits_at_once_on_empty_input() -> None:
    assert _serve() == ([], sys.path)
