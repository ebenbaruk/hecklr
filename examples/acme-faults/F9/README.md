# F9: end-of-speech threshold of 150 ms

`voice.yaml`: `end_of_speech_ms: 800` becomes `end_of_speech_ms: 150`. The agent decides the
caller has finished after 150 ms of silence, so it answers into the pauses callers make to think
in the middle of a sentence.

**Caught by** the false interruption rate: `voice_metric` `false_interruptions` (agent speech
starting inside a caller hesitation, per 100 caller turns) `below: 3` in the eight `catches-f9`
scenarios of the `voice-timing` suite (`hesitant-caller`, who pauses 550-650 ms in about half of
their sentences). The clean agent waits 800 ms and never starts inside a hesitation; F9 starts
talking about 500 ms into the pause (150 ms, then speech recognition), while the caller is still
thinking. The cut-up sentences often break the task too.

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --vad energy --fault F9 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
uv run hecklr compare --config examples/acme-voice/hecklr.yaml <clean run id> latest
```

`GET /healthz` on the agent reports `"variant": "F9"`. The whole voice matrix (the clean
agent twice, then each fault, each compared with the first clean run) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`; results are in
[`acme-voice/RESULTS.md`](../../acme-voice/RESULTS.md).
