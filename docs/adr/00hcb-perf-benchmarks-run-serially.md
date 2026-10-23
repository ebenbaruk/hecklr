# 00hcb. Performance benchmarks run in a serial CI step, skipped under xdist

Date: 2026-10-23 · Status: proposed · Workstream: WShcore (SC2) · Extends: ADR 0010b

## Context

SC2 says 200 scenarios x 3 trials finish in under 10 minutes at concurrency 32, "verified by a
perf benchmark". No test proved it. A throughput benchmark measures the machine as much as
Hecklr: under `pytest -n auto` every core runs another worker, so the same code is slower by a
factor that depends on what else the worker pool happens to run, and a wall-clock bound would be
either flaky or loose enough to prove nothing. The `perf` marker existed in `pyproject.toml`
("performance benchmark (SC2, SC3)") but nothing used it or kept it off the parallel run. SC4
timing had the same problem and solved it with ADR 0010b.

## Decision

- `perf` marks a test whose verdict is a throughput or capacity bound (SC2, SC3).
- The root `conftest.py` skips `perf` tests inside an xdist worker, with a reason that names the
  command to run them: `uv run pytest -m perf -p no:xdist`.
- CI runs that command as its own step, after the serial SC4 step and before the parallel suite,
  on the otherwise idle runner.
- `tests/perf/test_sc2_throughput.py` is the SC2 benchmark: the real compose path (`prepare_run`,
  `execute_run`), 200 guided scenarios (acme-maintenance's 64, repeated with new ids) x 3 trials
  at `budget.concurrency: 32`, the acme rules brain in its own process over HTTP/SSE calling its
  tools on per-trial world sandboxes, and `scripted_models` (FakeLLM) as simulator and judges. It
  asserts every trial and grade is `ok` and the wall time is under 600 s, and prints throughput
  (bootstrap interval over the gaps between trial finishes) and trial-duration median and p95
  (bootstrap intervals), so no number is reported bare.
- The bound is the spec's, never loosened. A run on a loaded machine that fails is rerun alone
  and both results are reported (ADR 0021a's procedure).

## Consequences

- SC2 is proven on every CI run, at a cost of one serial step (measured time in
  `packages/hecklr-core/PROGRESS.md`).
- FakeLLM answers instantly, so the benchmark measures Hecklr's own overhead plus a fast agent.
  With a real model, wall time is dominated by the provider's latency and rate limits, which SC2
  does not cover.
- A plain serial `uv run pytest` (no `-n`) also runs the benchmarks.
- The two `perf` tests that already existed, WS1's 1,000 fake trials under 60 s and
  hecklr-voice's SC3 50 calls, move from the parallel run to the serial step too, where their
  bounds measure Hecklr instead of the worker pool. The step collects coverage
  (`--cov --cov-append`) so the coverage gate still counts them.
