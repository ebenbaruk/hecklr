# 0024-hvoice. SC4 over WebRTC: what is asserted, and how it is run without a dedicated runner

Date: 2026-10-25 · Status: accepted · Workstream: hvoice (Wave 6) · Issues: #59, #27 ·
Extends: [0010b](0010b-sc4-timing-asserted-serially.md), [00hfa](00hfa-root-causes-of-the-flaky-adapter-tests.md)

## Context

SC4 says measured response latency is within ±20 ms of the injected delay, checked against the
loopback calibration agent. Hecklr proves it on the networked loopback agent, `hecklr calibrate`
and the four WebSocket transports (raw PCM 16 and 24 kHz, Twilio, Pipecat protobuf) on every
gate. Two voice contract cases go over WebRTC: `livekit` (a LiveKit SFU in Docker) and
`pipecat-smallwebrtc` (Pipecat 1.13's SmallWebRTC transport on aiortc). Their reference bots run
on the test process's event loop or a thread of it, so their own record of when they spoke is
the timing truth, and on a shared CPU that truth wanders by hundreds of milliseconds. ADR 0010b
therefore skips their timing checks on shared CPUs (`HECKLR_SHARED_CPU=1`), and #59 asked for a
dedicated runner. There is none: GitHub Actions is off for the private repository (ADR 0022a).

WebRTC also adds a delay of its own. Measured on an idle laptop (M2 report; offsets are measured
minus injected minus the bot's own lateness):

| Case | Latency offset (ms) | Barge-in offset (ms) |
| --- | --- | --- |
| livekit | +113 to +179 | +130 to +159 |
| pipecat-smallwebrtc | +210 to +334 | +226 to +258 |

This is Opus encoding plus the receivers' adaptive jitter buffers. A caller on a WebRTC line
hears it, so it is not an error of Hecklr's clock: it is latency the agent's users experience.

**#27** asked why the SmallWebRTC bot needs a larger allowance. ADR 00hfa traced both symptoms.
The "lost" ~110 ms of caller speech was aiortc's receiver holding a stream's last frames (its
jitter buffer, capacity 16, prefetch 4) until more packets arrive; the contract now keeps the line
open, as the driver does, and the allowance for it is gone. The late reply is Pipecat's
`RawAudioTrack.recv`: it paces frames from the wall-clock time the track started and never drops
one, so after its thread is starved it sends every overdue 10 ms frame at once. The caller's
playout plays the burst back to back (as a phone that never drops audio would), and because
Pipecat fills quiet time with silence frames the line never goes quiet, so the delay lasts for the
rest of the call. That is upstream behaviour, not our bot's code or Hecklr's measurement:
`tests/contract/test_pipecat_bot.py::test_issue27_pipecats_output_track_sends_overdue_frames_in_a_burst`
drives the real Pipecat track, and `test_issue27_a_burst_on_a_line_that_never_goes_quiet_...`
reproduces the delay through Hecklr's playout. Avoiding it in the bot would mean replacing
Pipecat's output track or running the bot in its own process (which removes GIL contention, not
CPU contention, and needs IPC for the call log the contract reads).

## Decision

- **What is asserted over WebRTC** (unchanged contract, `voice_cases.py`): every latency and
  barge-in offset lies within the case's transport window, widened by the 20 ms tolerance
  (livekit 0-350 ms, pipecat-smallwebrtc 0-400 ms; barge-ins also allow the 20 ms frame already
  in the earpiece), and all offsets of a run agree within 2 x 20 ms plus the case's jitter
  allowance (60 ms livekit, 200 ms SmallWebRTC). So over WebRTC Hecklr reports the agent's delay
  plus a bounded, steady transport delay; the strict ±20 ms claim is the loopback agent's and the
  WebSocket transports'.
- **How it is run**: `scripts/sc4-webrtc.sh [log-dir]` runs every contract test of the two cases
  in one process with `HECKLR_SHARED_CPU` unset (the dedicated-CPU opt-in), prints the load before
  and after, and fails if any case is skipped (the livekit case needs Docker). It is run by hand on
  a machine with idle cores before a release and whenever a WebRTC adapter changes, and its result
  goes in the PR. `voice-timing.yml` has a manual `webrtc-sc4` job that runs the same script, for
  when Actions is enabled on a larger or self-hosted runner.
- **#27**: the SmallWebRTC allowance stays (`jitter_ms=200`) as Pipecat's documented behaviour;
  the voice guide explains it.

## Runs of the script (2026-10-25, 10-core M1 Pro shared with other builders)

| Load (1 min) before → after | Result | Offsets, min to max (ms): livekit latency / barge-in; SmallWebRTC latency / barge-in |
| --- | --- | --- |
| 12.3 → 8.3 | 2 failed (livekit latency and barge-in), 16 passed | livekit 107-411 / 231-1,262; SmallWebRTC 210-403 / 275-380 |
| 3.9 → 8.5 | **18 passed**, 2 skipped (WebRTC bots cannot fail abnormally) | livekit 105-136 / 142-179; SmallWebRTC 208-405 / 228-257 |

The loaded run is why the cases need idle cores; the quiet run is the WebRTC SC4 result. In it
eight of the nine SmallWebRTC latency offsets lie within 208-216 ms and one is 405 ms: a reply
heard ~190 ms late, Pipecat's catch-up burst described above, inside the 240 ms spread the
contract allows.

## Consequences

- SC4 over WebRTC has a single command and a recorded result instead of an open issue, but no
  automatic gate: a WebRTC timing regression is caught when the script is run, not on every merge.
- Hecklr users testing a WebRTC agent should expect its response latency to include 100-350 ms of
  transport delay; the voice guide says so.
- If Pipecat ever drops overdue frames (or offers a setting to), the SmallWebRTC allowance can
  shrink to LiveKit's; `test_issue27_pipecats_output_track_sends_overdue_frames_in_a_burst` pins
  today's behaviour, so an upgrade that changes it fails there first.
