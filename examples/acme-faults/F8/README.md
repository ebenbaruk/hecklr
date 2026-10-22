# F8: barge-in disabled (the agent talks over the caller)

`voice.yaml`: `barge_in: true` becomes `barge_in: false`. The agent keeps talking when the
caller speaks over it and answers the interruption only after finishing its sentence.

**Caught by** the barge-in reaction metric: `voice_metric` `barge_in_reaction` (caller starts
speaking over the agent to agent audio stopping, p95 per call) `below: 400` in the eight
`catches-f8` scenarios of the `voice-timing` suite (`interrupter`, who cuts in on every reply).
The clean agent stops about 130 ms after the caller's first syllable (80 ms of speech counts as
a barge-in); F8 never stops, so the reaction is the rest of its sentence, seconds long.

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --vad energy --fault F8 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
uv run hecklr compare --config examples/acme-voice/hecklr.yaml <clean run id> latest
```

`GET /healthz` on the agent reports `"variant": "F8"`. The whole voice matrix (the clean
agent twice, then each fault, each compared with the first clean run) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`; results are in
[`acme-voice/RESULTS.md`](../../acme-voice/RESULTS.md).
