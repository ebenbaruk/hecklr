# 00hva. Hecklr runs its own DSP worker pool instead of `anyio.to_process`

Date: 2026-10-23 · Status: accepted · Workstream: hvoice (Wave 5) · Issues: #31, #47

## Context

ADR 0009a puts every utterance-sized DSP job (synthesis, impairments, codecs, ASR) in worker
processes, through `anyio.to_process.run_sync`. Two of anyio's fixed behaviors broke Hecklr, and
anyio offers no setting or hook for either:

1. **The parent's main file is re-run in every worker** (as `__mp_main__`, after deleting
   `sys.modules["__main__"]`). duckdb's C module refuses to import without a `__main__` module,
   so any program whose entry script imports duckdb (directly or through `hecklr_core`) gets
   `BrokenWorkerProcess` on its first voice job (#31). The `hecklr` console script hides its own
   file from workers; SDK users' scripts still failed (reproduced: an entry script importing
   duckdb, `anyio.to_process.run_sync(os.getpid)` -> `ImportError('initialization failed')`).
2. **Workers idle for 300 s are retired**, checked when the next job is dispatched, and idle
   workers are reused most recently used first. The workers warmed before calls are exactly the
   ones that sit idle and get retired; the next peak starts fresh workers that load faster-whisper
   or Kokoro on a caller's answer path, 4-7 s late (#47). Reproduced with
   `WORKER_MAX_IDLE_TIME = 0.05`: of 5 warmed workers, 0 survived three idle periods.

Options considered:

- **Keep-warm job** every minute on every worker (what the acme voice agent did). Needs a
  long-lived task owner, which a library call path does not have; LIFO dispatch does not
  guarantee each job lands on a distinct worker; and it does nothing for #31.
- **Patch anyio's module state** (`WORKER_MAX_IDLE_TIME`, or deleting `__main__.__file__` as the
  CLI does). Mutates a dependency or the user's own entry module for the whole program.
- **A small pool of our own** with anyio's protocol and semantics. Chosen.

## Decision

`hecklr_voice.pool` launches `python -u -m hecklr_voice._dsp_worker` with anyio's
`open_process`. The worker adopts the parent's `sys.path` (sent first), never runs the parent's
main file, and keeps its own module as `__main__`. Requests and replies are length-framed pickles,
like anyio's. Semantics kept from anyio: half-the-cores `CapacityLimiter`, most recently used
worker first, a job's exception re-raised with its own type, a dead worker raises
`BrokenWorkerProcess` and is replaced on the next job, cancelling a caller kills that worker, and
the loop's shutdown kills every worker through the hook anyio uses for its own pool
(`AsyncBackend.setup_process_pool_exit_at_shutdown`, imported from `anyio._core`).

Differences: workers are never retired for idling (a pool never exceeds the limiter's size, so
this costs at most `cpu_count / 2` idle processes), the worker sends fds 1 and 2 to the null
device at the OS level (so native libraries cannot corrupt replies or fill an unread pipe), and
`warm_dsp_pool` now refills the pool on every call (a no-op when full) instead of once per loop,
so a worker killed by a cancelled job is replaced before the next call, not during it.

## Consequences

- Any entry point may import duckdb and needs no `if __name__ == "__main__"` guard for voice.
  A DSP job must live in an importable module (never in the entry script); every job in the
  repository already does.
- No keep-warm machinery is needed anywhere. The acme voice agent's `keep_warm` and the CLI's
  `_keep_dsp_workers_off_the_entry_script` are now redundant; they stay until their owners'
  packages (`bench/`, `packages/hecklr`) drop them, because tests and the LiveKit bench agent
  import them.
- One private anyio import. A change there fails the pool's tests (`test_pool.py`) and every voice
  test under `filterwarnings = error`, not silently.
