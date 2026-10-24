# acme-voice and voice-loopback progress

Owner: WS11 (voice reference agent, networked loopback, faults F7-F10, SC3/SC4). Spec:
`docs/SPEC.md` → "Voice simulation engine", "Agent adapters", "Fault-injection matrix", SC3,
SC4, US-06, US-07. ADRs: 0006, 0007e, 0009a, 0010a, and this workstream's 0011a. Evidence:
[RESULTS.md](RESULTS.md).

## Done

- **Networked loopback** (`examples/voice-loopback`): `pcm` WebSocket agent with known reply
  delay and barge-in reaction, run in its own process; `/healthz` reports its own send lateness
  and CPU. Tests: `test_call.py` (timing decisions on a virtual clock), `test_server.py`
  (protocol violations close with 1003, health, CLI), `test_sc4_network.py`
  (`test_sc4_latency_and_barge_in_within_20ms_over_the_network[300|600|900]`, the agent as a
  subprocess), `test_bench.py`.
- **SC4 over the network** (`sc4_calibrate.py`): 840 measurements, worst error 13 ms.
- **SC3 load** (`sc3_load.py`): 150 simultaneous calls, 0 underruns; fails at 200 (the harness's
  single event loop is the bottleneck).
- **Voice reference agent** (`examples/acme-voice/agent/acme_voice`): `pipeline.py`
  (endpointing, floor-taking barge-in, superseded replies, one speculative transcription at a
  time, model jobs that finish on hang-up, handoff hang-up), `server.py` (protocol, world from
  start > header > base URL > local, `/healthz`), `config.py` (`voice.yaml` + overlays),
  `normalize.py`, CLI. Tests: `test_pipeline.py`, `test_server.py`, `test_config.py`. The agent
  hears callers with hecklr-voice's own ASR, which decodes words only since #48 (its earlier
  words-only `asr.py` is gone). Its `keep_warm` job is redundant since hecklr-voice's pool never
  retires workers (#47, ADR 00hva); it stays while `bench/stacks/agents/livekit_acme.py` imports
  it.
- **Faults F7-F10** (`examples/acme-faults/F7..F10`): one changed line of `voice.yaml` each
  (`test_sc8_each_voice_fault_changes_exactly_one_line_of_voice_yaml`,
  `test_sc8_each_voice_fault_flips_exactly_its_setting`), READMEs naming the catching metric.
- **Suites**: `voice-timing` (24 scenarios), `voice-snr` (13 x 5 SNR buckets), `voice-snr-10db`;
  personas `steady-caller`, `interrupter`, `hesitant-caller`, `cafe-caller-*`.
- **Driver-level matrix in CI**:
  `test_sc8_clean_voice_agent_passes_and_each_timing_fault_fails_its_metric` (real graded calls
  over localhost with scripted ears: clean passes; F7, F8, F9 each fail exactly their metric).
- **Through the CLI** (on a local merge with WS-wire's PR #32): `scripts/voice-fault-matrix.sh`
  exits 0. A/A `no_change` on both suites; F7, F8, F9, F10 each a suite regression
  (p 0.008-0.016) with only their own tag slice flipped. US-07 curve: clean agent 92/100/85/62/8%
  at clean/20/10/5/0 dB, F10 31/31/31/23/0%.
- **Re-run on `main` (5c75dd4, 2026-10-23/24)**: `scripts/voice-fault-matrix.sh all` exits 0 with
  the same verdicts (A/A no change / inconclusive; F7-F10 regression, p 0.008-0.009), and the
  US-07 curve reproduces within one task per cell; run ids in `docs/milestones/M2.md`.
- Numbers: 98 tests in the two examples, 90% coverage; whole repository 1996 passed.

## Next

- Add a suite-loading test through `hecklr.config.load_project_config` (the frozen schema could
  not load `type: websocket` voice adapters before WS-wire).
- Pipecat SmallWebRTC transport for the voice agent (the spec's P0 voice adapter); today it
  speaks the WebSocket `pcm` protocol only.

## Open questions

- `hecklr-voice`'s docstring and the spec's repo layout name the loopback `loopback-voice`; the
  WS11 brief owns `examples/voice-loopback`. Rename one or the other.
- Harness-side issues found here: #43 (`CachedTTS` race), #44 (`ScriptedSimulator` counts barge-in
  interjections), #47 (idle DSP workers retired), #48 (timestamped whisper decode 6-9 s).

## Decisions

- See ADR 0011a.
