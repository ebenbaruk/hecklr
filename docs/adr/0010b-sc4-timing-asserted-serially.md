# 0010b. SC4 timing is asserted in a serial CI step, skipped under xdist

Date: 2026-10-22 · Status: accepted · Workstream: WS10 (issue #50)

## Context

SC4 says measured voice timings (response latency, barge-in reaction) are within ±20 ms of the
truth, checked against the loopback agent in CI on every change (spec "Voice simulation engine >
Calibration and self-tests"). The tests that prove it place real-time calls over real sockets,
WebRTC stacks and agent subprocesses, so they measure the machine as well as Hecklr.

CI and local runs use `pytest -n auto`: every core runs a worker, and the reference agents share
CPUs (and in-process bots share the GIL) with other workers. One scheduler hiccup put a single
sample at 26.4 ms (`websocket-pcm-24k-burst`, main CI on d299274) and failed the run; locally the
voice contract, `examples/voice-loopback` and `hecklr`'s wired voice run each failed once and
passed on rerun. Main went red twice and the email PR was reverted (#30, #52).

The workarounds had already drifted: `timing_needs_idle_cpu` skipped one contract case under
xdist; `test_cli_calibrate` accepted a failing verdict under xdist; `test_voice_calibration`
asserted a 10x bound. Because CI only ran under xdist, the strict branches never ran in CI at all:
SC4 was weaker in CI than the spec says.

Options considered:

1. **Widen the bound** or judge a robust statistic (median/p90) under load. Rejected: SC4 is a
   per-measurement ±20 ms claim, and a tolerant statistic hides real single-turn errors (a
   transport that occasionally adds 40 ms is exactly what a user would hear).
2. **Retry flaky tests.** Rejected: retries make a real regression look intermittent.
3. **Assert SC4 strictly where the CPU is idle, and only there.** Chosen.

## Decision

- A `sc4_timing` marker labels every test whose verdict is SC4 wall-clock accuracy: the voice
  contract's SC4 latency and barge-in tests (every case), `hecklr-voice` driver SC4 tests,
  `examples/voice-loopback` network and calibration tests, `hecklr calibrate --voice-loopback`,
  the in-process calibration, the wired voice run in `hecklr`, and the driver's backchannel
  reaction check, which holds a real-time reaction to the same 20 ms.
- The root `conftest.py` skips `sc4_timing` tests inside an xdist worker with a reason that names
  the command to run them: `uv run pytest -m sc4_timing -p no:xdist`.
- CI runs that command as its own step before the parallel suite, on the otherwise idle runner,
  with coverage appended into the main step's report. The bound is unchanged and per sample; the
  ad-hoc xdist branches and the 10x bound are removed, so every SC4 test asserts the full ±20 ms
  in CI on every PR (but see the WebRTC exception below). The step fails if it selects no tests
  (pytest exit 5).
- **Exception: the two in-process WebRTC cases** (`livekit`, `pipecat-smallwebrtc`,
  `timing_needs_idle_cpu`). Their reference bots share the test process and its GIL with the
  harness, so their own record of when they spoke is the timing truth, and on GitHub's shared
  4-vCPU runner that truth is unreliable. The first serial CI run measured livekit offsets from
  −242 to +405 ms, and a pipecat-smallwebrtc outlier of 423 ms against a 420 ms window. On a
  dedicated machine they pass serially (21/21 locally). CI sets `HECKLR_SHARED_CPU=1` for the
  step, which skips SC4 for these two cases only, as before this ADR (they were always skipped
  in CI). They are asserted with the same command on an idle developer machine. The SC4 claim
  itself (spec: the loopback agent) stays enforced in CI over WebSocket, Twilio and Pipecat
  WebSocket, the networked loopback agent and `hecklr calibrate`.

## Consequences

- SC4 is enforced in CI more strictly than before, and parallel runs no longer flake on it.
- `pytest -n auto` alone does not prove SC4; contributors run the serial command too
  (CONTRIBUTING.md). The serial step adds its own run time to CI.
- A new SC4 timing test must carry `@pytest.mark.sc4_timing`, or it will flake under xdist again.
