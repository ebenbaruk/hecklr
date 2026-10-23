# 0009a. Voice engine: clocks, segmentation, turn-taking events and where DSP runs

Date: 2026-10-21 · Status: proposed · Workstream: WS9

> Superseded in part by [00hva](00hva-own-dsp-worker-pool.md): DSP runs in Hecklr's own worker pool, not `anyio.to_process`.

## Context

ADR 0006 fixed what voice events mean (`utterance.latency_ms`, `interrupt.reaction_ms`, silence
reasons) and ADR 0004a how graders read them. It left to `hecklr-voice` how those numbers are
measured on a live call, which audio format adapters deliver, what counts as an agent turn, and
how 50 real-time calls share one process without late frames (SC3) while staying within ±20 ms
of the truth (SC4).

## Decision

- **One format.** Mono 16 kHz everywhere: caller frames are 20 ms of 16-bit PCM at 16 kHz;
  voice adapters must deliver 16 kHz `TimedFrame`s (a different rate is a harness error).
  Narrowband codecs work at 8 kHz internally and return to 16 kHz.
- **Clocks from frame counts.**
  - Send: frame `k` is due at `origin + 20 k` ms. Late wake-ups never shift later frames, so a
    10-minute call drifts < 5 ms. A frame sent a whole period late is an *underrun*; underruns
    are counted (`CallStats`) and logged as a `harness` error event.
  - Receive: the driver stamps each frame on arrival and maps `TimedFrame.sample_index` to trial
    time with earliest-arrival alignment (`MediaClock`), like an RTP receiver. `sample_index`
    must count the stream's media clock, silence included. `TimedFrame.t_ms` is not used:
    adapters cannot know the trial's origin.
  - Edges are sample-accurate: inside the first and last speech frame, speech starts at the
    first sample above -50 dBFS and ends after the last one. Caller edges come from the clean
    TTS audio (the ground truth of what was said), agent edges from the received audio.
- **Agent speech and turns.** A frame is speech when its RMS reaches -45 dBFS (`EnergyVad`;
  `SileroVad` optional). A segment ends after 250 ms without speech. Segments starting after a
  caller turn began form the reply to it; `latency_ms` is set only on that reply, and only if it
  started after the caller finished (an agent talking over the caller has no response latency;
  it shows in talk-over and false interruptions instead). Speech after the reply was transcribed
  (the agent resuming after a pause, a re-engagement prompt) is a new agent turn without latency.
- **Caller behavior.** Gaps, barge-ins, backchannels, hesitations, fillers and re-engagement
  silences are drawn per decision from `(trial seed, kind, ordinal)` (`TurnTakingPolicy`). The
  caller answers once the agent has been quiet for its gap. A barge-in is tried once per agent
  reply, if the agent is still talking at the drawn moment; it is a caller turn with a fixed
  interjection ("Wait, hold on a second."), added to the simulator's dialogue. A backchannel is
  not a turn (no utterance event). `reaction_ms` is the end of the agent's segment minus the
  caller's onset; for a backchannel it is set only if the agent stopped within 1 s.
- **DSP off the loop.** Synthesis, the impairment chain, codecs, ASR and VAD models run in
  anyio worker processes (Silero in a worker thread), at most half the cores at once, with model
  libraries capped at 2 threads and onnxruntime spinning disabled. Saturating every core stalled
  the event loop for 15+ ms; a mid-call `import onnxruntime` stalled it for 0.5 s, so models load
  before the call connects and the DSP workers are spawned up front. Per-frame work stays on the
  loop because it is cheaper than any hand-off: one RMS, one edge scan and two copies of 320
  samples. 50 in-process calls measured a worst send lateness of 15-17 ms, no underruns.
- **Recordings** go to `<run_dir>/audio/<trial_id>/<attempt ULID>/{caller,agent,mix}.wav`.
  Sample `n` is trial time `n / 16` ms, and directories are never reused, so a retried attempt
  cannot overwrite an earlier one.
- **Impairments** follow the spec order. Values the persona schema does not carry are constants:
  ±3% speed jitter per segment, Gilbert-Elliott loss with 40 ms mean bursts, repeat-and-fade
  concealment. Noise is procedural (license-free) under the spec's environment ids. A line
  codec the machine cannot run (AMR-NB, Opus need ffmpeg) is refused, never sent clean.
- **Failures** map as in ADR 0006a, so `hecklr-voice` depends on `hecklr-adapters` for
  `AgentError`. A silent agent is a `timeout` after `SessionContext.timeout_s`; an agent that
  closes its stream ends the call with `agent_ended`.

## Consequences

- SC4 is measured in-process on every test run, and SC3 by `pytest -m perf` on an idle machine.
- Voice adapters (WS10) live in `hecklr-adapters` and must not import `hecklr-voice`, or the two
  packages would depend on each other; they deliver 16 kHz frames with media-clock indices.
- Agent ASR runs once the agent's turn is over, so the caller's gap includes ASR, simulator and
  TTS time when those are slower than the persona's gap (reported as the caller's `turn_gap`).
- A long-lived harness should `gc.freeze()` after start-up: a full collection over a large heap
  pauses the loop for tens of milliseconds (35 ms measured under pytest).
