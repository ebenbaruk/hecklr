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
| Inbound | `EnergyVad` (default) or `SileroVad`; ASR `fake/tag`, `faster-whisper/<size>`, `deepgram/<model>`, `assemblyai/<model>` (streaming), `assemblyai-batch/<model>` (pre-recorded); recordings on the trial timeline |
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

## Cloud speech: AssemblyAI ASR and OpenAI TTS

Local Kokoro and faster-whisper stay the free default. To hear the agent with AssemblyAI and
give callers OpenAI voices, set the keys in the environment (`ASSEMBLYAI_API_KEY`,
`OPENAI_API_KEY`; `hecklr doctor` checks them by name) and name the providers:

```yaml
# hecklr.yaml
models:
  asr: assemblyai/universal-streaming-english   # or assemblyai-batch/universal-2
# personas/<persona>.yaml
voice:
  tts: { provider: openai, voice: coral }        # gpt-4o-mini-tts, cached per (text, voice, settings)
```

| `models.asr` | API | Price (Oct 2026) | Notes |
| --- | --- | --- | --- |
| `assemblyai/universal-streaming-english` | v3 WebSocket, one session per agent turn | USD 0.15/h of session | also `-multilingual`; `universal-3-6-pro` at 0.45/h |
| `assemblyai-batch/universal-2` | pre-recorded REST: upload, submit, poll | USD 0.15/h of audio | also `universal-3-5-pro` at 0.21/h; sends the persona's language |

The engine hands the ASR one whole agent turn once the VAD closes it, so streaming does not
start before the turn ends: each turn is sent as fast as AssemblyAI accepts it (it reads about
1.25x real time) and the session is drained with `Terminate`, the only message that flushes the
last transcript. `ForceEndpoint` is handled ahead of queued audio, so one session cannot be
reused across turns. Free AssemblyAI accounts open 5 new streaming sessions per minute, which
one call exceeds: the refused turn fails the trial with a message saying so (retrying did not get
through within a turn's deadline). Use `assemblyai-batch/...` there: it has no session limit.

OpenAI TTS (`provider: openai`) uses `gpt-4o-mini-tts`, asks for 24 kHz PCM and resamples it to
16 kHz; voices are OpenAI's built-in ones (`alloy`, `coral`, `marin`, ...), `rate` maps to
`speed` (0.25-4), and a pitch shift is refused. Hecklr meters only LLM spend, so ASR and TTS
cost is not counted against `budget.usd`.

To try the `examples/acme-voice` suites with cloud speech, start the agent with real speech
(`--tts kokoro`, since `fake/tag` only hears synthetic speech) and override the two settings
above in a copy of its `hecklr.yaml` and persona.

Measured on 2026-10-22 (`-m live`, `test_live_cloud_speech.py`; 8 Kokoro reference
sentences, mean 3.2 s, and 3 OpenAI `coral` sentences; WER with a 95% bootstrap interval):

| Audio | `assemblyai/universal-streaming-english` | `assemblyai-batch/universal-2` | `faster-whisper/tiny.en` |
| --- | --- | --- | --- |
| Kokoro, clean | 2.3% [0.0, 4.6] | 2.3% [0.0, 4.6] | 3.4% [0.0, 7.8] |
| Kokoro, cafe 10 dB | 2.3% [0.0, 4.6] | 2.3% [0.0, 4.6] | 4.5% [1.0, 8.9] |
| OpenAI TTS, clean | 4.8% [0.0, 14.3] | 0.0% [0.0, 0.0] | 0.0% [0.0, 0.0] |
| Time to hear a turn | 1.00-1.14x its length (3.5-3.7 s) | 0.76-0.94x (2.7-2.9 s) | local |

Because the caller only answers once it has heard the turn, cloud ASR lengthens the caller's
reply gaps (silence attributed to the user, never to the agent): a 6.5 s agent turn was followed
by an 8.3 s caller gap in a `hecklr run` with streaming ASR. Agent metrics are unaffected; prefer
faster-whisper when caller-side pacing matters. Decisions: ADR 0009b.

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
uv run pytest packages/hecklr-voice -m live              # Kokoro + faster-whisper + Silero; cloud
                                                         # speech too when keys and a budget are set
```
