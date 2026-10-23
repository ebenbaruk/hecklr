"""The DSP process pool: utterance-sized audio work never runs on the event loop.

Synthesis, the impairment chain, codecs and model inference take milliseconds to seconds of CPU
per utterance. Run on the event loop, they would delay every other call's 20 ms frames (SC3), so
they go to worker processes, kept warm between calls.

At most half the cores run DSP at once. With every core busy, the OS stalls the event loop for
over 15 ms at a time (measured on a 10-core laptop), close to a whole frame period; with half,
under 1 ms. Even 50 calls need far less than half a modern machine for their DSP.

The pool follows ``anyio.to_process`` (same wire protocol, errors and cancellation) with two
differences anyio cannot be configured for (ADR 00hva):

- Workers never run the parent's main file. anyio's do, with no ``__main__`` module, where
  duckdb refuses to import: any program whose entry script imports duckdb (``hecklr_core`` does)
  could not start a worker (#31). Jobs must therefore live in importable modules.
- Workers are never retired. anyio retires a worker idle for five minutes and reuses the most
  recently used first, so the warmed workers were the ones retired, and their replacements
  loaded speech models in the middle of a call, seconds late (#47). Here a worker lives as long
  as the event loop, unless a job kills it or is cancelled.
"""

import contextlib
import os
import pickle
import subprocess
import sys
from collections.abc import Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import Final, Unpack, cast

import anyio
from anyio import BrokenWorkerProcess, CancelScope, CapacityLimiter

# Private, and what anyio.to_process itself uses: it kills the workers when the event loop
# shuts down, before their pipes are garbage collected with the loop already closed.
from anyio._core._eventloop import get_async_backend
from anyio.abc import ByteReceiveStream, ByteSendStream, Process
from anyio.lowlevel import RunVar
from anyio.streams.buffered import BufferedByteReceiveStream

_WORKER_COMMAND: Final = (
    sys.executable,
    "-u",
    "-P",
    str(Path(__file__).with_name("_dsp_worker.py")),
)
"""The worker file run directly, not with ``-m``: ``-m hecklr_voice._dsp_worker`` would import the
whole package (numpy, scipy, ...) before the handshake, seconds on a loaded machine. ``-P`` keeps
this directory off ``sys.path``, where its modules would shadow top-level names."""
_START_TIMEOUT_S: Final = 20.0
_HEADER_LIMIT: Final = 50
_STREAM_ERRORS: Final = (
    anyio.BrokenResourceError,
    anyio.ClosedResourceError,
    anyio.DelimiterNotFound,
    anyio.EndOfStream,
    anyio.IncompleteRead,
    ValueError,
)


def dsp_workers() -> int:
    """How many DSP jobs may run at once: half the cores, at least one."""
    return max(1, (os.cpu_count() or 2) // 2)


@dataclass(eq=False, slots=True)
class Worker:
    """One worker process and its reply stream."""

    process: Process
    replies: BufferedByteReceiveStream

    async def exchange(self, request: bytes) -> tuple[bool, bytes]:
        """Send one framed request; return (succeeded, pickled reply).

        Raises:
            BrokenWorkerProcess: the worker died or broke the protocol.
        """
        requests = cast(ByteSendStream, self.process.stdin)
        try:
            await requests.send(b"%d\n" % len(request) + request)
            status, length = (await self.replies.receive_until(b"\n", _HEADER_LIMIT)).split(b" ")
            if status not in (b"RETURN", b"EXCEPTION"):
                raise ValueError(f"unexpected reply {status!r}")
            return status == b"RETURN", await self.replies.receive_exactly(int(length))
        except _STREAM_ERRORS as exc:
            raise BrokenWorkerProcess(f"DSP worker {self.process.pid} failed") from exc


@dataclass(eq=False, slots=True)
class Pool:
    """The DSP workers of one event loop."""

    limiter: CapacityLimiter
    idle: list[Worker] = field(default_factory=list)
    """Most recently used last."""
    processes: set[Process] = field(default_factory=set)
    """Every live worker, idle or busy."""
    starting: int = 0
    """Workers being launched, not yet in ``processes``."""

    async def run(self, request: bytes) -> tuple[bool, bytes]:
        """Run one pickled job on an idle worker, or a new one if none is idle."""
        async with self.limiter:
            worker = await self._take_idle() or await self._start()
            try:
                reply = await worker.exchange(request)
            except BaseException:
                # Cancelled mid-job (or broken): the worker cannot be reused.
                await self._discard(worker)
                raise
            self.idle.append(worker)
            return reply

    async def fill(self) -> None:
        """Start one worker if the pool is not full; holds a slot so it never overshoots."""
        async with self.limiter:
            if len(self.processes) + self.starting < self.limiter.total_tokens:
                self.idle.append(await self._start())

    async def _take_idle(self) -> Worker | None:
        while self.idle:
            worker = self.idle.pop()
            if worker.process.returncode is None:
                return worker
            await self._discard(worker)  # exited while idle (killed from outside)
        return None

    async def _start(self) -> Worker:
        self.starting += 1
        try:
            process = await anyio.open_process(
                _WORKER_COMMAND, stdin=subprocess.PIPE, stdout=subprocess.PIPE
            )
        finally:
            self.starting -= 1
        self.processes.add(process)
        worker = Worker(process, BufferedByteReceiveStream(cast(ByteReceiveStream, process.stdout)))
        try:
            with anyio.fail_after(_START_TIMEOUT_S):
                await worker.exchange(pickle.dumps(sys.path, protocol=pickle.HIGHEST_PROTOCOL))
        except BaseException:
            await self._discard(worker)
            raise
        return worker

    async def _discard(self, worker: Worker) -> None:
        self.processes.discard(worker.process)
        with CancelScope(shield=True):
            with contextlib.suppress(ProcessLookupError):
                worker.process.kill()
            await worker.process.aclose()


_POOL: RunVar[Pool] = RunVar("hecklr_voice_dsp_pool")


def current_pool() -> Pool:
    """This event loop's pool, created on first use."""
    try:
        return _POOL.get()
    except LookupError:
        pool = Pool(CapacityLimiter(dsp_workers()))
        get_async_backend().setup_process_pool_exit_at_shutdown(pool.processes)
        _POOL.set(pool)
        return pool


async def run_dsp[*Args, R](func: Callable[[Unpack[Args]], R], *args: *Args) -> R:
    """Run ``func(*args)`` in a worker process and return its result.

    ``func`` must be importable by the worker (a module-level function of an importable module,
    never one defined in the program's entry script), and its arguments and result picklable.
    The job's own exception is raised here; a worker that dies raises ``BrokenWorkerProcess``.
    Cancelling the caller kills the worker mid-job rather than leaving it to finish unobserved.
    """
    request = pickle.dumps((func, args), protocol=pickle.HIGHEST_PROTOCOL)
    ok, reply = await current_pool().run(request)
    value = pickle.loads(reply)  # noqa: S301 -- our own worker, over a private pipe
    if not ok:
        raise value
    return cast(R, value)


async def warm_dsp_pool() -> None:
    """Start every DSP worker that is not running (all of them on first use).

    Spawning a worker costs the parent tens of milliseconds of CPU on the event loop, so do it
    before calls start, not in the middle of one. Cheap when the pool is full.
    """
    current = current_pool()
    async with anyio.create_task_group() as group:
        for _ in range(dsp_workers() - len(current.processes) - current.starting):
            group.start_soon(current.fill)
