# WS11 results: SC4 over the network, SC3 load, the voice fault matrix F7-F10, US-07

Machine: MacBook (Apple silicon, 10 cores), macOS, shared with three other builders' test runs
(load average noted per run). All runs are free: local models (Kokoro, faster-whisper, Silero),
the rules brain, scripted callers and deterministic graders, except the one realistic run at the
end (LLM brain on `openai/gpt-6-luna`, about $0.005). Every number carries its 95% interval.

## SC4: latency and barge-in accuracy over a real network transport

`uv run python examples/voice-loopback/sc4_calibrate.py --calls 10` (2026-10-22 07:26, load 4.8).
The loopback agent runs in its own process; Hecklr's `VoiceDriver` calls it through the
`websocket` adapter (`pcm`) over 127.0.0.1, five calls at a time, 10 plain and 10 barge-in calls
per case. Error = measured minus injected; intervals bootstrap over calls.

| Injected delay / reaction | Latency: n, bias, \|error\| p95, worst | Barge-in: n, bias, \|error\| p95, worst |
| --- | --- | --- |
| 300 ms / 150 ms | 140, +4 [+3, +4] ms, 7 [6, 9] ms, **12 ms** | 110, +4 [+3, +4] ms, 7 [7, 9] ms, **11 ms** |
| 600 ms / 300 ms | 140, +3 [+3, +4] ms, 7 [6, 8] ms, **13 ms** | 110, +3 [+3, +4] ms, 8 [7, 10] ms, **13 ms** |
| 900 ms / 200 ms | 140, +4 [+3, +4] ms, 8 [6, 10] ms, **13 ms** | 110, +3 [+3, +4] ms, 8 [6, 9] ms, **11 ms** |

**SC4 holds**: 840 measurements, worst error 13 ms (gate ±20 ms), none missing. The +3-4 ms bias
is the agent's own send lateness plus one frame of playout rounding; the agent reports its worst
lateness (8.9 ms) in `/healthz`. The same check runs in CI as
`test_sc4_latency_and_barge_in_within_20ms_over_the_network[300|600|900]`.

## SC3: simultaneous calls from one harness

`uv run python examples/voice-loopback/sc3_load.py --calls 50 100 150` then `200 300 400`
(07:32-07:39, load 2.2-5.2). One harness process (one event loop, as `hecklr run`), a fresh
loopback agent per level in its own process, half the callers barging in on every reply.

| Calls | Underruns (calls) | Worst send lateness | Worst loop lag | Latency \|error\| p95, worst | Barge-in \|error\| p95, worst | CPU cores: harness loop / DSP / agent |
| --- | --- | --- | --- | --- | --- | --- |
| 50 | **0** (0) | 16.4 ms | 23.7 ms | 3 [3, 4] ms, 6 ms | 3 [3, 3] ms, 5 ms | 0.31 / 0.32 / 0.15 |
| 100 | **0** (0) | 12.4 ms | 11.5 ms | 4 [2, 5] ms, 5 ms | 2 [2, 3] ms, 4 ms | 0.44 / 0.36 / 0.21 |
| 150 | **0** (0) | 19.6 ms | 23.2 ms | 5 [4, 5] ms, 12 ms | 3 [3, 4] ms, 11 ms | 0.59 / 0.50 / 0.29 |
| 200 | 33,698 (200) | 92 ms | 66 ms | 31 [28, 34] ms, 61 ms | 42 [35, 48] ms, 200 ms | 0.66 / 0.69 / 0.33 |
| 300 | 229,657 (300) | 1,506 ms | 122 ms | 1,031 ms, 1,182 ms | 2,357 ms, 5,873 ms | 0.54 / 0.58 / 0.29 |

**SC3 holds with headroom**: 150 simultaneous calls with no frame underrun and SC4-grade accuracy
(spec: 50 on 8 cores). The ceiling is between 150 and 200 calls. The bottleneck is the harness's
single event loop, not CPU: at 200 calls it averages only 0.66 of a core, but it handles about
20,000 frame events a second (send pacing, WebSocket framing, playout, segmentation per frame
per call) and their bursts delay frames past a period. Sharding calls across harness processes
would raise it. An earlier 100-call run on a loaded machine (load 12-28) with WebSocket
compression on showed 2,399 underruns; the loopback now turns permessage-deflate off (PCM does
not compress; it cost both sides CPU per packet).

## The voice fault matrix through `hecklr run` + `hecklr compare` (SC8, F7-F10)

`examples/acme-voice/scripts/voice-fault-matrix.sh` (06:55-07:25, load 4.1-4.7): the clean agent
twice (A/A), then each fault, each compared with the first clean run. Agent: `--brain rules
--tts synthetic` (`--vad energy` for the timing suite); callers speak Kokoro, the agent hears them
with faster-whisper `base.en`. Exit status 0.

| Comparison | Suite pass rate: baseline → candidate | Δ [95% CI] | p | Verdict | Fault's tag slice |
| --- | --- | --- | --- | --- | --- |
| `voice-timing` clean vs clean | 100.0% [86.2, 100] → 100.0% [86.2, 100] | 0.0 [0.0, 0.0] pts | 1.00 | **no change** | all `no_change` |
| F7 (+700 ms before TTS) | 100.0% → 66.7% [46.7, 82.0] | −33.3 [−54.2, −16.7] | 0.009 | **regression** | `catches-f7` 100% → 0%, q = 0.014 |
| F8 (barge-in off) | 100.0% → 66.7% [46.7, 82.0] | −33.3 [−54.2, −16.7] | 0.008 | **regression** | `catches-f8` 100% → 0%, q = 0.015 |
| F9 (150 ms end of speech) | 100.0% → 66.7% [46.7, 82.0] | −33.3 [−54.2, −16.7] | 0.008 | **regression** | `catches-f9` 100% → 0%, q = 0.015 |
| `voice-snr-10db` clean vs clean | 84.6% [57.8, 95.7] → 84.6% | 0.0 [0.0, 0.0] | 1.00 | **no change** | |
| F10 (weak ASR) | 84.6% → 30.8% [12.7, 57.6] | −53.8 [−76.9, −23.1] | 0.016 | **regression** | `catches-f10` −53.8 pts, q = 0.016 |

Each fault flips exactly its own scenarios and leaves the other areas `no_change`. On its metric:

| Run | Response latency p50 / p95 | Barge-in reaction p50 / p95 | False interruptions per 100 turns | Talk-over s/min |
| --- | --- | --- | --- | --- |
| clean | 710 [701, 749] / 782 [776, 784] ms | 91 [81, 93] / 108 [97, 111] ms | 0.0 [0.0, 0.0] | 0.3 [0.2, 0.5] |
| F7 | 1,417 [1,405, 1,454] / 1,485 [1,482, 1,489] ms | 92 / 105 ms | 0.0 | 0.3 |
| F8 | 757 / 783 ms | **9,059 [8,244, 10,078] / 14,544 ms** | 0.0 | **2.7 [1.1, 4.6]** |
| F9 | 392 / 448 ms (answers too soon) | 93 / 108 ms | **15.6 [6.2, 27.1]** | 0.5 |

F7's latency comparison alone finds the injected delay: Δp50 = +703 ms [665, 744], p = 0.0002.
The clean agent answers in 710 ms from the end of the caller's speech: 800 ms of endpointing on
the agent's VAD, minus the quiet tail of Kokoro's speech that Hecklr's -50 dBFS edge counts as
speech and the agent's -45 dBFS energy VAD does not.

Getting here took six CLI matrix runs; what the earlier ones exposed is now fixed and tested:
six scenarios per fault could not survive one noisy clean scenario (8 per fault now, 13 for
F10); Kokoro's own padding made 550-650 ms pauses measure 667-799 ms of silence, over the clean
800 ms endpoint (hesitations are 450-550 ms now); early transcriptions piled up per pause (one at
a time now); a hang-up killed a loaded speech worker (jobs finish now); anyio retired idle
workers after five minutes (kept warm now); and the timestamped whisper decode took 6-9 s on two
utterances (words-only decode now, 0.40 s; #48).

## US-07: the degradation curve (`voice-snr`, 13 tasks x 5 cafe-noise buckets)

`hecklr run --suite voice-snr` for the clean agent (run `01M5H1ZFYHPBH45MXK57RRQ3FR`) and F10
(`01M5H2JHJA866EEKDSKT2922QX`), then `hecklr compare` (verdict: regression). Task success:

| Caller SNR | Clean agent (`base.en`) | F10 (`tiny`) |
| --- | --- | --- |
| clean | 92.3% [66.7, 98.6] | 30.8% [12.7, 57.6] |
| 20 dB | 100.0% [77.2, 100] | 30.8% [12.7, 57.6] |
| 10 dB (gated) | 84.6% [57.8, 95.7] | 30.8% [12.7, 57.6] |
| 5 dB | 61.5% [35.5, 82.3] | 23.1% [8.2, 50.3] |
| 0 dB | 7.7% [1.4, 33.3] | 0.0% [0.0, 22.8] |

The clean agent degrades gracefully to 10 dB and collapses at 0 dB, where speech and chatter are
equally loud. The weak recognizer garbles spoken ids and postcodes at every level.

## The reference agent with every real model

The agent's defaults (LLM brain on `openai/gpt-6-luna`, Silero, `base.en`, Kokoro voice
`af_heart`) on the 8 latency scenarios, Hecklr hearing it with faster-whisper `base.en`
(`models.asr`; run `01M5H5J8N8X137562FYB416SS9`): 7 of 8 tasks done (`state`), every call
ended normally, and response latency 7,876 ms p50 [7,358, 8,772], because luna reasons before
each step (about 8 s a turn, ADR 0007d). The latency and dead-air gates fail, as they should for
an agent that slow on the phone. Agent LLM spend about $0.005; Hecklr's own spend $0.

## Tests and coverage

- `uv run pytest examples/voice-loopback examples/acme-voice -n 6`: 98 tests, including the SC4
  network test and the driver-level F7-F9 matrix (real graded calls over localhost, models
  replaced by test doubles); the whole repository: 1996 passed, 10 skipped.
- Coverage: 90% line and branch over both examples (`--cov=examples/voice-loopback
  --cov=examples/acme-voice/agent`); the uncovered lines are the CLIs' serve loops, which the
  SC4 test and the CLI matrix run as subprocesses.
- ruff, ruff format and `mypy --strict` clean for both examples (see each README for the
  `MYPYPATH`).
