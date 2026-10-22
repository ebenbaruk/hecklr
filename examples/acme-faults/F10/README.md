# F10: a weak speech recognizer

`voice.yaml`: `asr_model: faster-whisper/base.en` becomes `asr_model: faster-whisper/tiny`. The
multilingual `tiny` model is a fifth of the size and garbles numbers: customer ids and postcodes
come out as "142" or "75,5" instead of "C-1042" and "75005".

**Caught by** the degradation curve at 10 dB SNR: the task-success (`state`) criteria of the 13
`catches-f10` scenarios of the `voice-snr-10db` suite (the curve's gated bucket: a caller in a
busy cafe, chatter 10 dB below the voice). The clean agent completes them; F10 cannot identify
the customer in most of them. `voice-snr` runs every bucket (clean, 20, 10, 5, 0 dB) for the
whole curve.

## Run it

```bash
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --fault F10 --port 8765
uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-snr-10db --budget-usd 0.5
uv run hecklr compare --config examples/acme-voice/hecklr.yaml <clean run id> latest
```

`GET /healthz` on the agent reports `"variant": "F10"`. The whole voice matrix (the clean
agent twice, then each fault, each compared with the first clean run) is
`examples/acme-voice/scripts/voice-fault-matrix.sh`; results are in
[`acme-voice/RESULTS.md`](../../acme-voice/RESULTS.md).
