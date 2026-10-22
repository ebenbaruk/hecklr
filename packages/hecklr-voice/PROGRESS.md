# hecklr-voice progress

Owner: WS9. Spec: `docs/SPEC.md` → "Voice simulation engine", SC3, SC4, US-06, US-07.
Decisions: `docs/adr/0009a-voice-engine-timing-and-dsp.md`.

## Done

- **Audio basics** (`audio`, `synth`, `tag`): 16 kHz mono format, levels, sample-accurate
  speech edges, seeded streams; deterministic speech-like synthesis; LSB text tag read back by
  `FakeASR`. Tests: `test_audio.py` (hypothesis round trips, tag survives any cut).
- **Outbound**: `TTS` Protocol; `SyntheticTTS`, `KokoroTTS` (extra `local`), Cartesia /
  ElevenLabs / OpenAI over HTTP (key-gated, mocked in tests); `CachedTTS`; fillers and
  hesitation pauses from `TurnTakingPolicy`. Tests: `test_speech.py`.
- **Impairment chain** (`impair`, `noise`, `codec`, `network`) in spec order, seeded:
  `test_noise_is_mixed_within_half_a_db_of_the_target_snr` (7 environments x 4 SNRs),
  `test_render_clip_is_deterministic_in_its_seed`, `test_mulaw_matches_g711_reference_codes...`,
  `test_gilbert_elliott_matches_rate_and_mean_burst`, Opus round trip (AMR-NB commands verified
  in a `linuxserver/ffmpeg` container; the test skips where ffmpeg lacks the encoder, as on CI).
- **DSP pool** (`pool`): anyio worker processes, half the cores, warmed before calls;
  `test_dsp_pool_never_blocks_the_event_loop` (loop thread CPU < 5 ms between 5 ms heartbeats
  while 8 noisy 5 s clips render).
- **Inbound**: media clock, `CallRecorder` (caller, agent, stereo mix WAVs on the trial
  timeline), `EnergyVad` / `SileroVad`, `FakeASR` / `WhisperASR` / `DeepgramASR`, `wer`,
  `measure_asr` (corpus WER with a bootstrap `Estimate`), `reference_samples`.
- **Turn-taking** (`turntaking`): `test_turn_taking_statistics_match_persona_params`.
- **VoiceDriver** (`driver`, `call`, `streams`, `timing`): drift-free pacing
  (`test_sc4_ten_minute_call_has_no_cumulative_clock_drift`), events per ADR 0006
  (`test_events_follow_adr_0006_timing_and_links`), limits, agent failure mapping.
- **Loopback** (`testing.LoopbackVoiceTarget`) and SC4:
  `test_sc4_response_latency_within_20ms_of_injected_delay` (300/600/900 ms),
  `test_sc4_barge_in_reaction_within_20ms_of_injected_reaction` (150/300 ms).
- **SC3 (engine share)**: `test_sc3_fifty_simultaneous_calls_without_underruns` (`-m perf`):
  50 in-process calls, 0 underruns, worst send lateness 15-17 ms, latencies 496-511 ms for 500.
- **Real models** (`-m live`): Kokoro → faster-whisper `tiny.en` WER 4.5% clean; cafe noise
  WER 4.5% at 20 and 10 dB, 11% at 5 dB, 42-46% at 0 dB; Silero detects Kokoro speech.
- Coverage of the package: 96% (`uv run pytest packages/hecklr-voice -n auto --cov=...`).

- Issue #20 (WS-wire): `test_dsp_pool_never_blocks_the_event_loop` measures loop CPU per 1 ms
  beat (a 5 ms beat summed several ~1 ms IPC steps, up to 6.2 ms on a loaded machine), bound
  unchanged at 5 ms, plus a load-invariant check: loop CPU over two renders without the heartbeat
  is under half of one render's CPU. Issue #31: DSP workers cannot start when the parent's main
  module imports duckdb (fixed for the `hecklr` console script; the general fix is here).

## WS-asr (Wave 3): AssemblyAI ASR, OpenAI TTS

Done (decisions: ADR 0009b):

- `asr_assemblyai.AssemblyAIASR` (`assemblyai/<speech_model>`): one Universal-Streaming v3
  session per agent turn, drained with `Terminate`; the free tier's session limit fails the turn
  with a pointer to the batch provider. `AssemblyAIBatchASR` (`assemblyai-batch/<model>`):
  pre-recorded upload, submit, poll. Both from `ASSEMBLYAI_API_KEY` via `make_asr`.
  Tests: `test_asr_assemblyai.py` (local WebSocket server speaking the v3 sequence: turn merging,
  frame sizes 50-1000 ms, errors, close codes, HTTP refusal, timeout) and
  `test_asr_assemblyai_batch.py` (mocked REST); the module is 100% covered.
- OpenAI TTS (`openai`, `gpt-4o-mini-tts`, built in WS9) checked against the current API and run
  live. `hecklr doctor` checks the ASR's extra or key by name
  (`test_asr_key_is_checked_by_name_for_voice_projects`).
- Live (`test_live_cloud_speech.py`, 3 passed, about one cent): WER on 8 Kokoro sentences, clean
  and cafe 10 dB: streaming 2.3% / 2.3%, batch 2.3% / 2.3%, faster-whisper `tiny.en` 3.4% / 4.5%;
  OpenAI TTS speech: 4.8% / 0.0% / 0.0%. Time to hear a turn: streaming 1.00-1.14x its length,
  batch 0.76-0.94x.
- `hecklr run` on two acme-voice latency scenarios (rules brain, Kokoro agent voice), callers on
  OpenAI TTS: with streaming ASR (1 scenario, free-tier limit) and with batch ASR (2 scenarios),
  0 harness errors, every agent turn heard correctly, visit booked. The failing gates are the
  agent's (latency p95 4.2 s with Kokoro on CPU; its own whisper missed a postcode).
- Issue #57: concurrent first download of one model in a process crashes (models.py, WS9).

Open: a frame-fed ASR protocol would let streaming ASR hear the agent while it talks and remove
the caller-side delay; ASR/TTS spend is documented, not metered against `budget.usd`.

## Next

- WS10: voice adapters (Pipecat, LiveKit) deliver 16 kHz `TimedFrame`s with media-clock
  `sample_index` (ADR 0009a) and pass the contract suite with `VoiceDriver`.
- WS11: networked loopback agent for the SC3/SC4 CI job, F7-F10.
- WS7: compose `VoiceDriver` per channel; report `measure_asr` per SNR bucket next to US-07.

## Open questions

- `TimedFrame.t_ms` cannot be on the trial clock (adapters do not know the trial origin); the
  driver ignores it and stamps arrival itself. Worth a docstring RFC on `hecklr-schemas` later.
- Barge-in content is a fixed interjection; letting the simulator write it would need the
  agent's partial transcript mid-turn.

## Decisions

- See ADR 0009a: one 16 kHz format, frame-count clocks, 250 ms segment hangover, latency only
  on the first reply turn, half-the-cores DSP pool with 2-thread models, recordings per attempt.
