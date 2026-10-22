# F7: +700 ms of artificial delay before speaking

`voice.yaml`: `tts_pre_delay_ms: 0` becomes `tts_pre_delay_ms: 700`. Every reply waits 700 ms
before it is synthesized, as a slow TTS vendor or an extra network hop would make it.

**Caught by** the voice response latency gate: `voice_metric` `response_latency` (end of the
caller's speech to the agent's first audio, p95 per call) `below: 1200` in the eight
`catches-f7` scenarios of the `voice-timing` suite (`steady-caller`, who never pauses or
interrupts). The clean agent answers in about 650 ms (600 ms endpointing plus processing, worst
about 1 s on a busy machine); F7 can never answer in less than 1.3 s (600 + 700 ms plus
processing), so every latency scenario flips and `hecklr compare` reports a regression. The
gate sits above the spec's typical 1,000 ms because endpointing alone spends 600 ms of it, and
machine load only ever adds latency: 1,200 ms keeps the clean agent clear of it.

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --fault F7 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
```

`GET /healthz` on the agent reports `"variant": "F7"`. The whole voice matrix (clean twice,
then each fault, each compared with the clean baseline) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`.
