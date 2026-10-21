# hecklr-voice

Hecklr voice engine: audio pipeline, impairments, turn-taking, voice metrics.

Part of [Hecklr](../../README.md). Owned by WS9; see `docs/SPEC.md` ("Voice simulation engine")
and `docs/adr/0009a-voice-engine-timing-and-dsp.md`.

```python
from functools import partial
from hecklr_voice import VoiceDriver, make_asr, make_tts

driver = VoiceDriver(
    target,  # a VoiceTarget from hecklr-adapters
    asr=make_asr(config.models.asr or "faster-whisper/small"),
    tts=partial(make_tts, cache_dir=Path(".hecklr/cache/tts")),
    run_dir=run_dir,  # caller/agent/mix WAVs per trial
)
result = await driver.run(ctx, brief, simulator, sink)  # hecklr_schemas.ChannelDriver
```

Programs that run calls must guard their entry point with `if __name__ == "__main__":`: DSP runs
in worker processes, which import the main module.

| Stage | What it does |
| --- | --- |
| TTS | `synthetic` (deterministic, tagged, for tests and calibration), `kokoro` (local), `cartesia`, `elevenlabs`, `openai` (keys from the environment); cached by (text, voice, settings) |
| Impairments | speed jitter, procedural noise at an SNR (`car_interior`, `street`, `construction`, `cafe`/`babble`, `tv`, `crying_child`), synthetic reverb (`small_room`, `office`, `living_room`, `bathroom`, `hall`), telephone band-pass + G.711 mu-law (numpy) / AMR-NB / Opus (ffmpeg), Gilbert-Elliott loss with concealment, send jitter |
| Transport | drift-free 20 ms pacing on the send clock; agent frames placed on a media clock |
| Inbound | `EnergyVad` (default) or `SileroVad`; ASR `fake/tag`, `faster-whisper/<size>`, `deepgram/<model>`; recordings on the trial timeline |
| Turn-taking | persona gaps, barge-in, backchannels, hesitations, fillers, re-engagement silences, all seeded from the trial seed |
| Events | `utterance` (with `latency_ms`, `audio_span_id`), `audio_span`, `interrupt` (with `reaction_ms`), `silence` (`turn_gap`, `hesitation`, `re_engagement`) per ADR 0006 |

Measuring Hecklr's own hearing (reported next to agent results so ASR errors are never blamed on
the agent):

```python
samples = await reference_samples(
    make_tts("kokoro"), ImpairmentChain(environment="cafe", snr_db=10), voice="af_sarah", seed=1
)
quality = await measure_asr(make_asr("faster-whisper/small"), samples)  # quality.wer is an Estimate
```

`hecklr_voice.testing.LoopbackVoiceTarget` is an in-process agent that replies a known delay after
the caller stops and stops a known delay after a barge-in: the SC4 calibration target.

## Extras and models

- Base install: numpy, scipy, anyio, httpx2. Everything above works with `synthetic` TTS, `fake`
  ASR and the energy VAD.
- `pip install 'hecklr-voice[local]'`: Kokoro (kokoro-onnx; its phonemizer uses GPL espeak-ng,
  which is why it is optional), faster-whisper and Silero VAD (onnxruntime). Models download on
  first use into `.hecklr/models`.
- AMR-NB and Opus lines need `ffmpeg` with `libopencore_amrnb` / `libopus`; a persona asking for
  one on a machine without it is refused with `VoiceConfigError`.

## Tests

```bash
uv run pytest packages/hecklr-voice -n auto              # unit + SC4 calibration (real time)
uv run pytest packages/hecklr-voice -m perf              # SC3: 50 simultaneous calls, alone
uv run pytest packages/hecklr-voice -m live              # Kokoro + faster-whisper + Silero
```
