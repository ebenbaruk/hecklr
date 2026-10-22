# acme-voice and voice-loopback progress

Owner: WS11 (voice reference agent, networked loopback, faults F7-F10, SC3/SC4). Spec:
`docs/SPEC.md` → "Voice simulation engine", "Agent adapters", "Fault-injection matrix", SC3,
SC4, US-06, US-07. ADRs: 0006, 0007e, 0009a, 0010a, and this workstream's 0011a.

## Done

- **Networked loopback** (`examples/voice-loopback`): `pcm` WebSocket agent with known reply
  delay and barge-in reaction, run in its own process; `/healthz` with its own send lateness
  and CPU. Tests: `test_call.py` (timing decisions on a virtual clock), `test_server.py`
  (protocol violations close with 1003, health, CLI), `test_sc4_network.py`
  (`test_sc4_latency_and_barge_in_within_20ms_over_the_network[300|600|900]`, the agent as a
  subprocess), `test_bench.py`. 90% coverage of the example.
- **SC4 over the network** (`sc4_calibrate.py`): see `RESULTS.md`.
- **SC3 load** (`sc3_load.py`): see `RESULTS.md`.
- **Voice reference agent** (`examples/acme-voice/agent/acme_voice`): pipeline (`pipeline.py`:
  endpointing, floor-taking barge-in, superseded replies, speculative transcription, handoff
  hang-up), `server.py` (protocol, world resolution start > header > base URL > local,
  `/healthz`), `config.py` (`voice.yaml` + overlays), `normalize.py`, CLI. Tests:
  `test_pipeline.py`, `test_server.py`, `test_config.py`; 90% coverage of `agent/`.
- **Faults F7-F10** (`examples/acme-faults/F7..F10`): one changed line of `voice.yaml` each
  (`test_sc8_each_voice_fault_changes_exactly_one_line_of_voice_yaml`,
  `test_sc8_each_voice_fault_flips_exactly_its_setting`), READMEs naming the catching metric.
- **Suites**: `voice-timing` (24 scenarios), `voice-snr` (13 x 5 SNR buckets), `voice-snr-10db`;
  personas `steady-caller`, `interrupter`, `hesitant-caller`, `cafe-caller-*`.
- **Driver-level matrix in CI**:
  `test_sc8_clean_voice_agent_passes_and_each_timing_fault_fails_its_metric` (real graded calls
  over localhost with scripted ears: clean passes; F7, F8, F9 each fail exactly their metric).
- **Through the CLI**: `scripts/voice-fault-matrix.sh` runs `hecklr run` + `hecklr compare` for
  clean x2 and each fault (needs WS-wire's voice support in `hecklr run`, PR #32); results in
  `RESULTS.md`.

## Next

- After WS-wire (#32) merges: merge `origin/main`, rerun `scripts/voice-fault-matrix.sh` on an
  idle machine, and add a suite-loading test through `hecklr.config.load_project_config`.
- Pipecat SmallWebRTC transport for the voice agent (the spec's P0 voice adapter); today it
  speaks the WebSocket `pcm` protocol only.

## Open questions

- `hecklr-voice`'s docstring and the spec's repo layout name the loopback `loopback-voice`; the
  WS11 brief owns `examples/voice-loopback`. Rename one or the other.
- #43 (`CachedTTS` race on identical concurrent requests), #44 (`ScriptedSimulator` counts
  barge-in interjections as script lines).

## Decisions

- See ADR 0011a.
