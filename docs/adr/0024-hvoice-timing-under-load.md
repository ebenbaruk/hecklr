# 0024-hvoice. Timing tests that fail under host load are rerun alone by the gate

Date: 2026-10-25 · Status: accepted · Workstream: hvoice (Wave 6) · Issues: #121, #123, #94 ·
Extends: [0010b](0010b-sc4-timing-asserted-serially.md), [00hcb](00hcb-perf-benchmarks-run-serially.md),
[0022a](0022a-local-ci-is-the-merge-gate.md)

## Context

The merge gate runs on a shared 10-core laptop (ADR 0022a). Its `sc4-timing` and `perf` steps
already run serially, but nothing stops other work on the machine from taking the CPU: other
builders' test runs, a VM at 100%, the antivirus daemon at 50-90%, Spotlight. Three gates failed
on tests that pass when rerun alone:

- #121, `perf`: `test_sc3_fifty_simultaneous_calls_without_underruns` had 672 underruns and a
  worst send lateness of 282 ms at load 5.4-6.2 (with a VM at 100%); alone at load 3.1, 5 passed.
- #123, `sc4-timing`: `hecklr`'s `test_sc4_loopback_measures_every_setting` (latency errors up
  to 1,123 ms at load 2.7-3.5), then `test_ws_wire_voice_run_measures_latency_...`, then
  `examples/voice-loopback`'s calibration case (41 underruns at load 25). Each passed alone.
- #94 failed in the parallel `tests` step and is not a load problem, so this ADR does not cover
  it: a gate started as a background job (`cmd &`, nohup) passes an ignored SIGINT to the CLI
  child the Ctrl-C test spawns, asyncio.run installs no handler then, and the run ends 0. The
  test now restores the default handler in the child and always spawns it with SIGINT ignored.

The cause is the same in every timing case. The caller's audio is placed on a 20 ms send grid
and every timing is computed from that grid (ADR 0009a). When the harness process does not get
the CPU, its frames leave late (an underrun); the agent hears the caller late, and the measured
latency includes the harness's own lateness. The driver already reports this on the trial
("caller frame(s) left a frame period or more late; voice timings may be skewed"). SC3 measures
exactly that lateness. No test change can make a process meet real time on a CPU it is not
given, and the usual isolation is not available here: an unprivileged process can only lower its
priority, macOS has no CPU affinity, and there is no dedicated runner (Actions is off).

Options considered:

1. **Widen the bounds**, or judge a median. Rejected for the reasons in ADR 0010b: SC3 and SC4
   are per-frame and per-measurement claims.
2. **Retry every failure.** Rejected (ADR 0010b): a real regression would look intermittent.
3. **Refuse to run timing steps above a load threshold.** The gate would then never finish on
   a busy afternoon, and the threshold would hide nothing that a rerun does not show.
4. **Rerun only the failed timing tests, alone, once the load has dropped, and report both
   results.** This is the manual procedure of ADR 0022a, made automatic and narrow. Chosen.

## Decision

- `scripts/ci-local.sh` runs `sc4-timing` and `perf` through `timing_step`. When one fails, the
  tests its log names as `FAILED` or `ERROR` are rerun, once, with the same command (coverage
  appended), after the 1-minute load average is at most `HECKLR_RERUN_MAX_LOAD` (default: half
  the cores) or `HECKLR_RERUN_WAIT_S` (default 900 s) has passed.
- The step passes only if the rerun passes. Both logs are kept (`<step>.log`,
  `<step>-rerun.log`), and the summary records both results with their load averages plus a
  `NOTE` line naming the steps that passed only on the rerun. The PR's "Local CI" comment quotes
  them, and a note goes on the timing issue, as ADR 0022a asks.
- No other step is rerun. A step whose log names no failed test (a crash, a coverage error) fails
  at once. Bounds are unchanged: SC3 zero underruns, SC4 ±20 ms.

## Consequences

- The gate stops going red because of other people's load, and every such case stays visible.
- A regression that only shows under load (a change that makes the harness slower, so that it
  underruns only when the CPU is contended) can pass on the rerun. The rerun log and the `NOTE`
  line keep it on record, and SC3's printed worst send lateness shows the margin shrinking. A
  deterministic timing regression (a fault like F7's +700 ms) fails both runs at any load.
- A dedicated idle runner remains the real fix. When Actions is enabled for a public repository,
  `voice-timing.yml` runs these steps on the runner it is given, without reruns.
