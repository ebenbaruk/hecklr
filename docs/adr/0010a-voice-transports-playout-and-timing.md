# 0010a. Voice transports: a playout clock, exact resampling, background reading

Date: 2026-10-21 · Status: proposed · Workstream: WS10

## Context

ADR 0009a fixed what the driver expects from a voice adapter: 16 kHz `TimedFrame`s whose
`sample_index` counts the receive stream's media clock, so that earliest-arrival alignment maps
samples to trial time. Real transports do not deliver that. A Twilio-style agent sends seconds of
TTS in one message and relies on the phone network to play it; Pipecat's WebSocket output and
WebRTC pace 20 ms packets; LiveKit and aiortc hand over 10 or 20 ms frames at 48 kHz and keep
their own queues; and a burst `clear`s when the caller barges in. SC4 asks for ±20 ms.

## Decision

- **A playout per call** (`voice/playout.py`), the caller's earpiece. Audio arriving on a quiet
  line starts at its arrival time; audio arriving while earlier audio plays queues behind it; a
  packet up to 40 ms late still joins its run (jitter), a later one starts a new run (the line
  went quiet). Frames are released when their first sample is due, so the driver's earliest-
  arrival offset is the playout origin and every `sample_index` is the time it was heard.
  `clear()` drops what has not played, which is how Twilio `clear` and Pipecat interruption
  frames stop the agent. The 20 ms frame already released plays out, as on a phone, so a
  clear-based stop is heard up to one frame late; Twilio `mark`s are echoed when their audio has
  played or been cleared.
- **Read in the background.** Each link starts an asyncio task at connect that reads its
  transport into the playout, so audio is stamped when it arrives, not when the driver next
  reads. Without it, a LiveKit stream queued during the driver's start-up showed up as 1.2-1.5 s
  of fake latency. Transport failures are stored and raised from `frames()` after the audio
  received before them has played.
- **Exact, low-delay resampling.** A windowed-sinc interpolator at exact rational positions with
  cached per-phase kernels: output sample `j` is the input at `j * rate_in / rate_out`, and the
  only delay is 8 input samples of look-ahead (0.5-1 ms). soxr's streaming mode, used by Pipecat,
  holds back 20-90 ms in blocks, which would have read as agent latency. G.711 mu-law is
  encoded and decoded in numpy (`audioop` is gone in Python 3.13).
- **Bursts are converted in 100 ms pieces** with a checkpoint between them, so a 6 s Twilio
  burst never holds the loop for more than ~0.3 ms (the contract measures < 5 ms stalls).
- **LiveKit** joins `<prefix>-<trial_id>` as `hecklr-caller-<trial_id>` with a self-signed token
  and takes the first remote audio track as the agent. Its `AudioSource` plays silence when a
  frame is late and keeps the backlog, so a timing hiccup would add latency for the rest of the
  call; the adapter drops quiet caller frames (peak below -50 dBFS) while more than 20 ms is
  queued, never speech. Direct capture (`queue_size_ms=0`) needs 10 ms pushes and distorted the
  caller's timing more.
- **Pipecat** is SmallWebRTC, as the spec and schema say: an aiortc peer POSTs its offer
  (`{"sdp", "type", "request_data": {"trial_id"}}`) to the configured `/api/offer` URL. The call
  is answered when the connection is up, not when the answer arrives (a hang-up before DTLS is
  up never reaches the bot). A data channel carries Pipecat's `peerLeft`. Pipecat agents behind
  a WebSocket use the `websocket` adapter's `pipecat` protocol (hand-encoded `frames.proto`,
  tested against Pipecat's own serializer).
- **The contract measures with the real driver** against a loopback agent whose truth is what it
  actually did (planned time plus its own measured send lateness), on its own loop in a thread.
  WebRTC cases allow a bounded transport delay (codec and jitter buffers are real latency a
  caller hears) and its drift during a call; WebSocket cases must be exact. WebRTC SC4 tests
  skip inside a parallel (xdist) run, because jitter buffers and the in-process reference bots
  react to CPU load; they run serially (`pytest .../test_voice_contract.py`).

- **`TimedFrame.t_ms` counts from connect, not from the trial start.** The frozen schema
  describes it as trial-clock time, but `SessionContext` carries no trial origin, so an adapter
  cannot know it. Adapters stamp milliseconds since `connect` on their own monotonic clock (as
  `hecklr-voice`'s in-process loopback does); the driver times everything from `sample_index`
  and its own clock and does not read `t_ms`. Filling it with true trial time needs the origin
  in `SessionContext` (a schema RFC), worth doing only when a consumer needs it.
- **Hang-up always releases the transport.** The goodbye message may fail or be cancelled (the
  driver bounds hang-ups at 5 s); closing the socket, peer connection or room and stopping the
  reading task run in a `finally`, shielded and bounded (`RELEASE_TIMEOUT_S`, and websockets'
  `close_timeout` of 1 s, since it advises against cancelling `close()`).
- **Pipecat negotiation uses the trial's `timeout_s`** for the offer POST: the frozen
  `PipecatAdapterConfig` has no timeout field, and the per-reply deadline is the longest a
  caller should wait for the line to pick up.

## Consequences

- WebSocket SC4 holds over real sockets for raw PCM (16 and 24 kHz), Twilio and Pipecat
  framing: measured latency and barge-in reaction within 20 ms of the agent's actual timing.
- Over LiveKit (local server) a caller hears 80-170 ms of transport delay on an idle machine
  (up to ~340 ms under load) that wanders by up to ~60 ms per call; over Pipecat SmallWebRTC
  ~210 ms, with one reply per long call up to ~150 ms later. Reports show these as the agent's latency, which is what its customers hear; gates
  should be set accordingly.
- The `websocket` voice config needs RFC 0010a to be selectable from `hecklr.yaml`.
