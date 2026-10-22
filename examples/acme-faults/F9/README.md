# F9: end-of-speech threshold of 150 ms

`voice.yaml`: `end_of_speech_ms: 600` becomes `end_of_speech_ms: 150`. The agent decides the
caller has finished after 150 ms of silence, so it answers into the pauses callers make to
think in the middle of a sentence.

**Caught by** the false interruption rate: `voice_metric` `false_interruptions` (agent speech
starting inside a caller hesitation, per 100 caller turns) `below: 3` in the six `catches-f9`
scenarios of the `voice-timing` suite (`hesitant-caller`, who pauses 300-450 ms mid-sentence).
The clean agent waits 600 ms and never starts inside a hesitation; F9 starts inside every one
(the cut-up sentences usually break the task too).

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --fault F9 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
```

`GET /healthz` on the agent reports `"variant": "F9"`. The whole voice matrix (clean twice,
then each fault, each compared with the clean baseline) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`.
