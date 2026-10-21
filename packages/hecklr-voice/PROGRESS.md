# hecklr-voice progress

Owner: WS9. Spec: `docs/SPEC.md` → "Voice simulation engine", SC3, SC4, US-06, US-07.
Decisions: `docs/adr/0009a-voice-engine-timing-and-dsp.md`.

## Plan

1. Audio basics (`audio`): 16 kHz mono float/int16, 20 ms frames, levels, SNR, resampling.
2. Outbound: `TTS` Protocol; `SyntheticTTS` (deterministic, speech-like, LSB text tag);
   `KokoroTTS` (extra `local`, download on demand); Cartesia/ElevenLabs/OpenAI over HTTP
   (key-gated); `CachedTTS` keyed by (provider, text, voice, settings).
3. Impairment chain (`impair`, `noise`, `codec`, `network`) in the spec order: speed jitter,
   procedural noise at a target SNR, synthetic reverb, telephone band-pass + codec (G.711 mu-law
   in numpy, AMR-NB / Opus via ffmpeg), Gilbert-Elliott loss with PLC.
4. Process pool (`pool.run_dsp`, anyio worker processes) for every utterance-sized DSP job.
5. Inbound: media clock from frame counts, `CallRecorder` (caller, agent, stereo mix WAVs),
   `Vad` Protocol (`EnergyVad`, `SileroVad`), `ASR` Protocol (`FakeASR`, `WhisperASR`,
   `DeepgramASR`), `wer` and `measure_asr`.
6. `TurnTakingPolicy`: seeded gaps, barge-in, backchannel, hesitation, fillers, re-engagement.
7. `VoiceDriver` (ChannelDriver for voice): drift-free 20 ms pacing, conversation loop, events
   per ADR 0006 / 0004a, limits, agent error mapping.
8. `hecklr_voice.testing`: in-process `LoopbackVoiceTarget` for unit tests and SC4 calibration.

## Done

- Empty package scaffold (Wave 0): src layout, `py.typed`, smoke test.

## Next

- Steps 1-8 above.

## Open questions

- None yet.
