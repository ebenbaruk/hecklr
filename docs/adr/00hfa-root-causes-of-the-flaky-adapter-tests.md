# 00hfa. Root causes of the flaky adapter and email tests

Date: 2026-10-23 · Status: accepted · Workstream: hflake (Wave 5; issues #73, #56, #70, #27)

## Context

Four tests made `main` red at random, and one contract case carried allowances nobody could
explain. Each was traced to its cause before anything was changed; no bound was widened.

- **#73 (1)** `test_ws10_contract_silent_agent_times_out[pipecat-smallwebrtc]` failed 3/3 on a
  machine with a link-local interface: an aioice STUN retry fired on a closed socket and raised
  inside the event loop. Two gaps in aiortc 1.15 / aioice 0.10: `Connection.close()` does not
  cancel a connectivity check the agent triggered after ICE completed, and a cancelled gathering
  leaves its STUN queries retrying. Separately, aiortc's default configuration queries Google's
  public STUN server, which made every local call wait 5 s for an interface with no route to it
  and sent unit-test traffic to the internet.
- **#73 (2)** the loop-free contract measured a heartbeat's lateness against all loop-thread CPU
  in its window. A Twilio or 24 kHz burst is converted in many short steps (each under 2 ms here),
  so on a loaded runner the legitimate work in one late window summed to 5.43 ms.
- **#56** the WebSocket chat adapter used the 0.5 s reply timeout for the opening handshake.
- **#70** a single failed IMAP poll ended an email trial as `agent_error`. F12 polls every 0.1 s
  for the whole 4 s reply deadline, so it was the case that hit a dropped connection. Dropping
  every third poll against real GreenMail reproduces the CI result exactly
  (`['agent_error', 'state']` instead of `['timeout', 'state']`).
- **#27** the Pipecat SmallWebRTC bot seemed to lose ~110 ms of each caller utterance and to
  deliver one reply per long call ~150 ms late.

## Decision

- **SmallWebRTC hang-up** waits for ICE gathering to end by itself (aioice bounds it at 5 s),
  cancels and awaits every connectivity check, then closes the peer and stops every ICE
  transport. The checks are reached through aioice's private `_check_list`, the only handle on
  them. An agent on a loopback host is called with no STUN server; any other agent keeps aiortc's
  default. Media and data share one transport (max-bundle), as on Pipecat's side.
- **The loop-free contract** times each event-loop step's CPU on the loop thread
  (`asyncio.events.Handle._run`, through which every callback and task step runs) and holds every
  step under the same 5 ms. A self-test proves the instrument catches a 6 ms step.
- **WebSocket chat handshake**: `WebSocketChatTarget(config, connect_timeout_s=10.0)`, the voice
  WebSocket default. The frozen `WebSocketAdapterConfig` has no field for it, so it cannot be set
  from `hecklr.yaml` yet; an RFC can add one if a user needs it.
- **IMAP reply polling**: after a trial's first successful poll, a failed poll is retried at the
  next interval. The stream fails when the first poll fails (a wrong or dead server, reported at
  once) or when polls have kept failing for `ctx.timeout_s`.
- **#27, the lost caller speech** was not lost: aiortc's encoder holds the last caller frame and
  its receiver's audio jitter buffer (capacity 16, prefetch 4) holds four more until further
  packets arrive. The contract stopped sending right after the speech, so the tail never came;
  the driver sends line audio all call long, so a real call only delays it. The contract now
  keeps the line open and the 150 ms `caller_loss_ms` allowance is gone: the case is held to
  20 ms like every other transport. `test_issue27_aiortc_holds_the_last_five_frames_...` pins the
  aiortc behaviour.
- **#27, the late reply** is real audio arriving late. The bot runs on a thread of the test
  process; when it is starved of CPU or the GIL, Pipecat's `RawAudioTrack` stops sending and then
  sends every overdue frame at once (measured: sends up to 100-385 ms late at load 10-30). The
  caller's playout plays the burst back to back, as a phone that never drops audio would, and
  since Pipecat fills quiet time with silence frames the line never goes quiet, so the delay
  lasts for the rest of the call. Neither our bot code nor the adapter mismeasures anything; the
  SmallWebRTC case keeps `jitter_ms=200` and stays out of CI's shared-CPU SC4 step (ADR 0010b).
  `tests/contract/test_pipecat_bot.py` reproduces both halves.

Not done: running the bot in its own process (removes GIL contention, not CPU contention, and
needs IPC for the call log the contract reads) and NetEq-style silence compression in the
playout (would change what Hecklr reports as heard latency for every WebRTC agent). Either
belongs in its own decision if the allowance ever needs to shrink.

## Consequences

- No ICE or STUN timer outlives a SmallWebRTC call; local calls connect in milliseconds instead
  of 5 s, and the adapter's unit tests no longer send packets to the internet.
- The loop-free contract is about steps, not windows: several short back-to-back steps that
  together delay a timer more than 5 ms no longer fail it.
- A dead IMAP server mid-trial is reported as the driver's reply timeout, not as an agent error,
  when the driver's reply deadline is shorter than `ctx.timeout_s`.
