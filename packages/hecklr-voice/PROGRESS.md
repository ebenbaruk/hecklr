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
- **DSP pool** (`pool`): worker processes (own pool since ADR 00hva), half the cores, warmed
  before calls;
  `test_dsp_pool_never_blocks_the_event_loop` (loop thread CPU < 5 ms between 1 ms heartbeats
  while 8 noisy 5 s clips render; a timing test, so `-m perf` and serial since hvoice).
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
  is under half of one render's CPU. Issue #31: DSP workers could not start when the parent's main
  module imported duckdb (fixed generally by the own pool, ADR 00hva).

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
- Issue #57: concurrent first download of one model in a process crashed (fixed in hvoice).

Open: a frame-fed ASR protocol would let streaming ASR hear the agent while it talks and remove
the caller-side delay; ASR/TTS spend is documented, not metered against `budget.usd`.

## hvoice (Wave 5): robustness, SC3 serial, WebRTC SC4 job

Done (decisions: ADR 00hva, ADR 0010b follow-up):

- #57, #81: `ensure_model_file` is safe under concurrent first downloads: a partial per call
  (pid + uuid), a per-path lock in-process (one request for 16 concurrent callers), and a lost
  rename with a complete target counts as success.
  `test_issue57_concurrent_first_downloads_in_one_process_all_succeed` (16 tasks, local HTTP
  server), `test_issue81_concurrent_first_downloads_across_processes_all_succeed` (4 processes x 4
  threads), `test_issue81_a_lost_rename_race_counts_as_success_when_the_target_is_complete`.
  The old code failed the first with 15 `FileNotFoundError`s.
- #43: `CachedTTS` writes a partial per write.
  `test_issue43_concurrent_synthesis_of_the_same_text_is_safe` (24 concurrent, identical audio).
- #31, #47: own DSP pool (`pool`, `_dsp_worker`): workers never run the parent's main file and
  are never retired for idling; anyio's protocol, errors, cancellation and shutdown otherwise.
  `test_issue31_workers_start_when_the_entry_script_imports_duckdb` (unguarded entry script,
  ran once), `test_issue47_idle_workers_are_never_retired` (warmed pids survive idle gaps; on
  anyio with `WORKER_MAX_IDLE_TIME = 0.05`, 0 of 5 survived), plus error/cancel/dead-worker
  tests in `test_pool.py`. Warming imports `hecklr_voice` in every worker
  (`test_warm_workers_have_the_voice_engine_imported_before_any_job`); without it a call's first
  DSP jobs paid the import mid-call, which shifted reply phases and made
  `test_recordings_hold_both_sides_on_the_trial_timeline` fail 5 of 16 concurrent runs (0 of 16
  on main, 0 of 32 with the fix).
- #48: `whisper_transcribe` decodes without timestamp tokens.
  `test_issue48_whisper_decodes_words_without_timestamps`;
  `test_issue48_words_only_decode_is_as_accurate_on_caller_turns` (local `base.en` from the
  Hugging Face cache + macOS `say`, offline: WER no worse than the timestamped decode) and
  `test_issue48_words_only_decode_keeps_up_with_real_time_on_caller_turns` (`-m perf`, serial
  only: 0.85-1.15 s CPU on 3.8-6.2 s lines at load ~11). On `say` lines the timestamped decode
  costs the same CPU (0.88-0.91 s), so the 6-9 s drop needs a captured Kokoro turn as a
  fixture: owed.
- #55: SC3 and the other voice timing tests (`-m perf` in hecklr-voice) are skipped under xdist
  and have their own serial CI step; #59:
  `.github/workflows/voice-timing.yml` (first run owed until Actions billing is restored).

## hvoice-close (Wave 6): voice cleanup and timing flakes

Done (branch `ws-hvoice-voice-and-flakes`; decisions: ADR 0024-hvoice-webrtc-sc4,
ADR 0024-hvoice-timing-under-load):

- #95: `examples/acme-voice/scripts/voice-fault-matrix.sh` writes `HECKLR_VOICE_PORT` into the
  adapter URL of a generated copy of `hecklr.yaml` (next to it, removed on exit), stops at the
  first run that exits non-zero, and has a `smoke` mode. Run for real:
  `HECKLR_VOICE_PORT=36571 ... voice-fault-matrix.sh smoke` -> run `01M5R0QYKPW6JJTTT1A2F7TRDF`,
  24 trials completed against the agent on 36571 (load 15-33), exit 0, generated config removed.
- #82: `keep_warm` (acme-voice), the LiveKit bench agent's pre-warm synthesis and the CLI's
  `_keep_dsp_workers_off_the_entry_script` are gone.
  `test_issue31_dsp_workers_start_under_an_entry_script_that_imports_the_cli` (hecklr) runs a DSP
  job from an entry script that imports `hecklr.cli`; SC3/SC4 serial results are in the PR's
  Local CI comment.
- #59: `scripts/sc4-webrtc.sh` (+ manual `webrtc-sc4` job in `voice-timing.yml`). At load 12.3
  -> 8.3: livekit SC4 failed (offsets 107-411 ms latency, up to 1,262 ms barge-in). At load
  3.9 -> 8.5: 18 passed, 2 structural skips; offsets livekit 105-136 / 142-179 ms, SmallWebRTC
  208-405 / 228-257 ms (latency / barge-in).
- #27: Pipecat's `RawAudioTrack` catch-up burst is upstream behaviour (ADR 00hfa tests); recorded
  in ADR 0024-hvoice-webrtc-sc4 and the voice guide; allowance kept.
- #94: the Ctrl-C test signals once the stub agent has a request in flight (5 s replies) and
  asserts the process is still running; the original rc 0 did not reproduce in 115 tries at load
  up to 55 (probe kept out of the tree).
- #121, #123: `scripts/ci-local.sh` reruns the failed tests of `sc4-timing` and `perf` alone once
  the load falls to half the cores (max 900 s wait), keeps both logs and reports both; exercised
  with fake steps (rerun passes, rerun fails, no failed test named).

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
