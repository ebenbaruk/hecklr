# F7: +700 ms of artificial delay before speaking

`voice.yaml`: `tts_pre_delay_ms: 0` becomes `tts_pre_delay_ms: 700`. Every reply waits 700 ms
before it is synthesized, as a slow TTS vendor or an extra network hop would make it.

**Caught by** the voice response latency gate: `voice_metric` `response_latency` (end of the
caller's speech to the agent's first audio, p95 per call) `below: 1300` in the eight
`catches-f7` scenarios of the `voice-timing` suite (`steady-caller`, who never pauses or
interrupts). The clean agent answers about 710 ms after the caller stops (p95 780 ms; its
800 ms endpointing starts at its own VAD's end of speech); F7 answers 700 ms later (1,417 ms
p50), so every latency scenario flips and `hecklr compare` reports a regression. The gate sits
above the spec's typical 1,000 ms because endpointing spends most of it, and machine load only
ever adds latency, so the margin is on the clean agent's side.

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --vad energy --fault F7 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
uv run hecklr compare --config examples/acme-voice/hecklr.yaml <clean run id> latest
```

`GET /healthz` on the agent reports `"variant": "F7"`. The whole voice matrix (the clean
agent twice, then each fault, each compared with the first clean run) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`; results are in
[`acme-voice/RESULTS.md`](../../acme-voice/RESULTS.md).
