# acme-voice: the reference agent over voice (WS11)

The acme-maintenance support agent on the phone: the same brain, tools and world as the chat
reference agent ([`../acme-maintenance`](../acme-maintenance/README.md), imported, never
copied), behind a cascaded voice pipeline:

```text
caller audio -> VAD (Silero | energy) -> endpointing -> ASR (faster-whisper) -> normalization
             -> acme brain (rules | LLM) + the trial's world tools -> TTS (Kokoro | synthetic) -> caller
```

It speaks the `pcm` WebSocket protocol of Hecklr's `websocket` voice adapter (RFC 0010a): a JSON
`start` message, 16 kHz 16-bit mono PCM both ways, a JSON `clear` when the caller barges in, and a
normal close when it hands the call to a human. Its tools act on the trial's world from the
`start` message's `world_url`, else the `X-Hecklr-World-Url` header, else `--world-base-url`, else a
private copy of the world (ADR 0007e). Everything runs locally and free. Decisions: ADR 0011a.

## The pipeline's settings: `voice.yaml`

| Setting | Clean | What it does | Fault |
| --- | --- | --- | --- |
| `end_of_speech_ms` | 800 | caller silence that ends their turn | [F9](../acme-faults/F9/README.md): 150 |
| `barge_in` | true | stop talking when the caller talks over the agent | [F8](../acme-faults/F8/README.md): false |
| `barge_in_min_speech_ms` | 80 | caller speech that counts as a barge-in | |
| `tts_pre_delay_ms` | 0 | wait before speaking | [F7](../acme-faults/F7/README.md): 700 |
| `asr_model` | `faster-whisper/base.en` | how the agent hears | [F10](../acme-faults/F10/README.md): `faster-whisper/tiny` |
| `vad` | `silero` | `silero` (noise-robust) or `energy` | |
| `tts_provider`, `tts_voice` | `kokoro`, `af_heart` | how the agent speaks | |

Transcription starts speculatively after 100 ms of caller silence, so the ASR is usually done
when the turn ends; the agent answers about 850 ms after the caller stops (800 ms of it is
endpointing, Pipecat's default stop time).

## Run it

```bash
uv sync --all-packages --all-extras            # local models download on first use
# the reference agent (LLM brain on openai/gpt-6-luna by default; ACME_MODEL=ollama/... is free)
uv run python examples/acme-voice/agent/serve.py --port 8765
# what the suites run: rules brain, agent speech Hecklr hears exactly for free (fake/tag)
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --port 8765
uv run python examples/acme-voice/agent/serve.py --brain rules --tts synthetic --vad energy \
    --fault F7 --port 8765                       # voice-timing callers are on clean lines
curl http://127.0.0.1:8765/healthz               # variant, pipeline, calls per world source

uv run hecklr run --config examples/acme-voice/hecklr.yaml --suite voice-timing --budget-usd 0.5
uv run hecklr compare --config examples/acme-voice/hecklr.yaml <clean run id> latest
examples/acme-voice/scripts/voice-fault-matrix.sh     # clean x2, F7-F10, with verdicts
```

Flags: `--brain`, `--model`, `--fault F7..F10` or `--overlay DIR`, `--vad`, `--asr`, `--tts`
(override `voice.yaml`), `--world-base-url`, `--models-dir`, `--host`, `--port`. `ACME_*`
variables configure the brain as for the chat agent; `ACME_FAULT=F3` runs a chat fault over voice.

## Suites (`hecklr.yaml`)

Scripted callers and deterministic graders: no LLM is called, the runs cost $0.

| Suite | Scenarios | Personas | Gate | Catches |
| --- | --- | --- | --- | --- |
| `voice-timing` | 8 `catches-f7` tasks (cancel, reschedule, book) | `steady-caller` | task done + `response_latency` p95 < 1300 ms | F7 |
| | 8 `catches-f8` | `interrupter` (barges in on every reply) | `barge_in_reaction` p95 < 400 ms | F8 |
| | 8 `catches-f9` tasks | `hesitant-caller` (450-550 ms pauses, British, slow) | task done + `false_interruptions` < 3 per 100 turns | F9 |
| `voice-snr` | 13 tasks | `cafe-caller-{clean,20db,10db,5db,0db}` | task done | the US-07 curve |
| `voice-snr-10db` | 13 tasks | `cafe-caller-10db` | task done | F10 |

The timing suite runs the agent on the energy VAD (`--vad energy`): its callers are on clean
lines, and the energy VAD costs no model and no thread hop per frame, so the agent's
turn-taking does not wander with machine load. The SNR suites need `voice.yaml`'s Silero.

## Tests

```bash
uv run pytest examples/acme-voice                  # unit, server, and the driver-level F7-F9 matrix
MYPYPATH=examples/acme-maintenance/agent:examples/acme-voice/agent:examples/acme-voice/tests \
  uv run mypy --strict examples/acme-voice
```

The tests need no model: callers speak `synthetic` audio and the agent's ears are a scripted
recognizer (`tests/voice_testing.py`). Results of the real runs: [RESULTS.md](RESULTS.md).
