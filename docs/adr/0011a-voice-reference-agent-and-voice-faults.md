# 0011a. Voice reference agent, voice faults F7-F10, and the networked loopback

Date: 2026-10-22 · Status: proposed · Workstream: WS11

## Context

SC8 needs a voice agent with four planted faults (F7 +700 ms before TTS, F8 barge-in off, F9 a
150 ms end-of-speech threshold, F10 a weak ASR) that Hecklr catches on the right metric, while
the clean agent shows no regression against itself. SC4 asks for ±20 ms latency and barge-in
accuracy over a real transport, SC3 for 50+ simultaneous calls. Hecklr's own deterministic
ears (`fake/tag` ASR, reading an LSB tag in `synthetic` speech) cannot serve the agent: the
harness normalizes every caller utterance's level, which destroys the tag.

## Decision

- **The acme brain over voice, not a second agent.** `examples/acme-voice` imports `acme_agent`
  (rules or LLM brain, same tools, same world resolution order) and adds a cascaded pipeline:
  VAD (Silero by default, energy for synthetic speech) -> faster-whisper -> transcript
  normalization (digit groups, spoken customer ids) -> brain -> TTS (Kokoro, or tagged
  `synthetic`). It speaks the `pcm` WebSocket protocol of RFC 0010a and reads the trial's world
  from the `start` message or the `X-Hecklr-World-Url` header (ADR 0007e).
- **One editable file, `voice.yaml`.** It holds every pipeline knob; F7-F10 are overlays in
  `examples/acme-faults/F7..F10` holding a `voice.yaml` with one line changed (tested). The chat
  agent's loader refuses them and the voice loader refuses chat overlays.
- **Turn-taking.** A caller *takes the floor* after 80 ms of continuous speech. With barge-in on,
  that sends `clear` (the harness's playout drops unplayed audio) and drops any reply still being
  prepared for an earlier turn. Replies are sent as one burst; the agent tracks when the caller
  will have heard them. Transcription starts speculatively after 100 ms of caller silence, so a
  batch recognizer is off the response path whenever the caller does not resume (measured: ASR
  wait 0 ms in most turns). Endpointing is 800 ms, Pipecat's default stop time.
- **Suites split by what they need.** In every suite the agent speaks `synthetic` audio, so the
  caller hears it exactly and for free (`models.asr: fake/tag`), while the callers speak Kokoro
  and the agent hears them with its real ASR. `voice-timing` gates latency (F7, `below: 1300`),
  barge-in reaction (F8, `below: 400`) and false interruptions (F9, `below: 3`), 8 scenarios
  each; `voice-snr` is the US-07 curve (13 tasks x clean/20/10/5/0 dB of cafe noise, one
  persona per bucket, ADR 0006) and `voice-snr-10db` its gated bucket, where F10 (`base.en` ->
  multilingual `tiny`) fails. The latency gate is above the spec's typical 1,000 ms because
  endpointing spends 800 ms of it and machine load only adds latency: F7 can never answer under
  1,500 ms. Hesitations are 450-550 ms pauses, 570-700 ms of silence once Kokoro's own padding
  is added (measured): the clean agent's 800 ms endpoint waits them out, and F9's agent (150 ms,
  then about 350 ms of ASR started at 100 ms) speaks about 450 ms in, while the caller is still
  thinking. A call runs one early transcription at a time, and speech-model jobs are never
  cancelled (anyio kills a cancelled job's worker). The agent gives every DSP worker a short
  job each minute: anyio retires workers idle for five minutes, last in first out, and the
  replacement loaded whisper on a caller's answer path (4-7 s, five minutes into every run). The timing suite runs the agent on the energy VAD (clean
  lines): Silero's per-window thread hop and its lag on speech offsets made the agent's
  endpointing wander with machine load.
- **CI without models.** A driver-level matrix runs real graded calls over localhost with the
  agent's ears replaced by `ScriptedASR`, which hears the next script line per *utterance*
  (keyed by its first 160 ms of sound, so the speculative and final transcriptions of one turn
  agree). It proves F7, F8 and F9 each fail exactly their metric. F10 needs real speech and is
  proven by `scripts/voice-fault-matrix.sh` through `hecklr run` and `hecklr compare`.
- **The networked loopback runs in its own process.** `examples/voice-loopback` places replies on
  the caller's media clock and paces 20 ms packets 40 ms ahead; a barge-in cut is decided when
  the caller's onset arrives, at least `MIN_REACTION_MS` (100 ms) ahead, so it lands at the
  sample. `/healthz` reports the agent's own send lateness, so calibration can separate agent
  lateness from measurement error. Both servers disable permessage-deflate: compressing PCM
  50 times a second per call showed up in load profiles.

## Consequences

- SC4 holds over a real socket (worst error 11 ms over 30 calls at 300/600/900 ms); SC3 is
  measured by `sc3_load.py` with machine load recorded next to the numbers.
- A voice fault is caught by `hecklr compare` as a suite regression only if enough scenarios flip
  to survive the sign-flip test and the slice correction: 6 flips (p = 2/2^6) did not survive
  one noisy clean scenario in the first CLI matrix, so each fault area has 8 scenarios (13 for
  F10, whose weak ASR does not fail every task).
- `ScriptedSimulator` counts barge-in interjections as script lines (#44), so barge-in scenarios
  gate the metric only; `CachedTTS` races on identical concurrent requests (#43) and costs a
  retry, not a result; the harness's own DSP workers face the same idle retirement (#47).
- Voice timings depend on the machine: the matrix should run on an otherwise idle machine, and
  the timing gates leave headroom for load in the direction it pushes.
