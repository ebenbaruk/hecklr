# 0009b. Cloud speech: AssemblyAI ASR (streaming and pre-recorded) and OpenAI TTS

Date: 2026-10-22 · Status: proposed · Workstream: WS-asr (Wave 3)

## Context

The user chose AssemblyAI as the cloud ASR (it has no standalone TTS) and OpenAI for cloud TTS,
with local Kokoro and faster-whisper staying the free default. The engine's `ASR` protocol
transcribes one whole agent turn once the VAD closes it (ADR 0009a); changing that to a
frame-fed stream would touch the call loop, which is outside this workstream. AssemblyAI's
real-time API is the Universal-Streaming v3 WebSocket. Three measurements on the user's account
(October 2026) shaped the design:

- A turn streamed as fast as AssemblyAI accepts it, then drained with `Terminate`, is
  transcribed well (WER 2.3% on the clean and 10 dB reference sets, faster-whisper `tiny.en`
  3.4% and 4.5%) in 1.00-1.14x its own length.
- `ForceEndpoint` sent after a burst of audio is handled ahead of the queued audio: it closed an
  empty turn before any speech, and the speech's turn was finalized only by a later message.
  Only `Terminate` flushes everything sent.
- The account is on the free tier: 5 new streaming sessions per minute. Retrying a refused
  session with 1-8 s backoff did not get through within a 34 s turn deadline; refused attempts
  appear to count against the window.

## Decision

- **`assemblyai/<speech_model>`**: one streaming session per agent turn. Send the turn's PCM in
  100 ms binary frames (the last one padded to the 50 ms minimum), send `Terminate`, read every
  `Turn` until `Termination`, and join the turns (a partial never replaces a final; an empty
  final never replaces words). A session is not reused across turns, because nothing marks the
  end of a turn's audio except `Terminate`. `format_turns=true` for `universal-streaming-*`; the
  pro models always format. Auth is the `Authorization` header (server-side, no temporary token).
- **No retry on the session limit.** The refusal fails the turn at once with a message naming the
  limit and `assemblyai-batch/universal-2` as the way out.
- **`assemblyai-batch/<speech_model>`**: the pre-recorded API (upload a WAV, submit with the
  persona language's primary subtag, poll every 250 ms). No per-minute session limit, the same
  WER, and turns come back in 0.76-0.94x their length.
- **OpenAI TTS** stays the existing `openai` provider (`gpt-4o-mini-tts`, 24 kHz PCM resampled to
  16 kHz, cached by `CachedTTS`); verified against the current API and exercised live.
- **Cost is documented, not metered.** Hecklr's budget meters LLM spend only (`hecklr-core`
  metering); AssemblyAI bills session time (streaming) or audio time (pre-recorded) at USD
  0.15-0.45 per hour, and OpenAI TTS bills per audio. Metering speech would need a new cost path
  through the voice driver, which no requirement asks for yet.
- `hecklr doctor` checks the configured (or default) ASR's extra or API key, by name.

## Consequences

- Cloud ASR lengthens the simulated caller's own reply gaps (silences attributed to the user, not
  the agent): about the agent turn's length plus the simulator and TTS. Agent metrics (latency,
  barge-in, dead air attributed to the agent) are measured on the harness clock and do not change,
  but an agent with a short "are you still there?" timer may re-engage. Faster-whisper remains the
  low-latency choice; a frame-fed ASR protocol would remove the delay and is the natural next step
  if cloud ASR becomes the main path.
- On a free AssemblyAI account, streaming works only for calls with at most about five agent
  turns per minute; `assemblyai-batch` works at any rate.
