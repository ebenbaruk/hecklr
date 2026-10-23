# Benchmark v1.0: measured rows (2026-10-23)

Seven of the eight stacks were measured with `hecklr bench run` on one MacBook (Apple silicon,
10 cores) shared with other builds; every row records the machine's 1-minute load average at its
start and end. Numbers are value [95% interval]; latencies in ms. The leaderboard
(`hecklr bench site`) shows the same rows with their details. Hecklr's customers and judges ran
on qwen3 4B on local Ollama (32k context); seed 20261023.

## Chat (8 guided scenarios x 2 trials)

| Stack | Task success | Judge: resolved | Judge: no hallucination | First token p50 | First token p95 | Verdicts | Load |
| --- | --- | --- | --- | --- | --- | --- | --- |
| `acme-chat-rules` | 78.6% [49.8, 99.8] | 87.5% [49.9, 99.9] | 50.0% [9.5, 90.5] | 31 [13, 347] | 1,873 [342, 3,440] | 12/16 | 20.6 → 6.1 |
| `acme-chat-qwen3` | 56.2% [24.9, 87.4] | 87.5% [50.0, 100.0] | 100.0% [43.9, 100.0] | 5,032 [4,479, 6,581] | 24,920 [7,725, 27,444] | 16/16 | 5.2 → 6.7 |

Both rows passed `handoff_appropriate` on the gas emergency (100% [20.7, 100], one scenario).
The rules row's 4 conversations without a verdict are judge calls that timed out while the
machine's load was over 20 (Ollama shared with other jobs); its first-token p95 is the same load,
not the agent (it answers in milliseconds). The 4B model completes fewer tasks than the keyword
brain on these scenarios, and takes about 5 s to the first token on a laptop GPU.

`acme-chat-luna` is not measured: the project's OpenAI credits ran out during its run (4 of 16
conversations failed with `429 insufficient_quota`). That run is why the runner now refuses a row
whose agent reports provider failures instead of publishing them as the stack's errors.

## Voice (9 scripted calls x 2 trials, same rules brain everywhere)

| Stack | Task success | Latency p50 | Latency p95 | Barge-in p50 | Barge-in p95 | Dead air /call | Cost /call (USD) | Verdicts | Load |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| `acme-voice-local` | 88.9% [56.5, 98.0] | 924 [898, 946] | 1,045 [998, 1,209] | 116 [105, 119] | 145 [131, 153] | 0.00 [0.00, 0.00] | 0 | 18/18 | 6.7 → 14.1 |
| `acme-voice-livekit` | 88.9% [56.5, 98.0] | 1,077 [1,054, 1,112] | 1,292 [1,179, 1,751] | 248 [239, 252] | 286 [265, 294] | 0.00 [0.00, 0.00] | 0 | 15/18 | 7.0 → 6.1 |
| `pipecat-local` | 77.8% [50.2, 100.0] | 3,123 [2,773, 3,758] | 7,185 [5,556, 11,222] | 676 [648, 844] | 1,662 [947, 1,892] | 3.00 [2.56, 3.44] | 0 | 17/18 | 5.7 → 7.0 |
| `pipecat-assemblyai` | 88.9% [56.5, 98.0] | 4,181 [2,829, 5,006] | 7,186 [6,681, 7,532] | 661 [510, 908] | 1,213 [865, 1,250] | 3.33 [2.72, 4.00] | 0.0024 [0.0018, 0.0030] | 16/18 | 6.1 → 4.7 |

No stack interrupted a hesitating caller (false interruptions 0.00 [0.00, 0.00] everywhere). The
`resolved` judge passed 80-83% of judged calls on every stack (intervals overlap).

What the rows say:

- **Transport.** The same cascade behind a LiveKit SFU answers about 150 ms later at p50 and
  reacts to a barge-in about 130 ms later than over a raw PCM WebSocket: WebRTC's jitter buffer
  and Opus round trip, on top of identical models and endpointing.
- **Pipecat at its defaults** answers 2-3 s later than the hand-tuned cascade and stops talking
  0.5 s later after a barge-in, with three silences over 2 s per call. Most of it is speech
  synthesis: in a traced call the brain had answered 0.7 s after the caller stopped and the first
  audio left 3.7 s after that. Pipecat's Kokoro service synthesizes each reply when it is needed,
  while the acme cascade transcribes speculatively and serves repeated replies from its on-disk
  cache.
- **Streaming ASR** (AssemblyAI) did not shorten Pipecat's response path here; its latency is in
  synthesis, not recognition. It cost USD 0.0024 per call at list price (USD 0.0437 for 18 calls).

The acme cascade's first run, with an empty reply cache, measured p95 7,708 [1,559, 11,174]:
long replies synthesized for the first time take seconds on CPU. The published row is warm, as
the cascade runs in production (its stack file says so). The `pipecat-assemblyai` row was
measured before the per-call spend fix (f25008c): its total is exact, each call's share was
attributed one call late.

## Calibration (3 calls x 3 trials)

| Stack | Latency p50 | Latency p95 | Barge-in p50 | Barge-in p95 |
| --- | --- | --- | --- | --- |
| `loopback-reference` (600 ms / 150 ms) | 550 [511, 562] | 599 [578, 609] | 180 [167, 183] | 189 [180, 189] |

With natural (Kokoro) caller speech the harness measures the 600 ms agent at 550 ms p50 and the
150 ms barge-in stop at 180 ms: Hecklr's speech edge (-50 dBFS) counts Kokoro's quiet tail and
onset as speech, the agent's energy threshold does not. With synthetic speech the same agent
measures within 13 ms (SC4, `examples/acme-voice/RESULTS.md`). Read the voice rows' latencies as
caller-perceived, about 50 ms shorter than an agent's own VAD would say.

## Reproduction (US-12)

`uv run hecklr bench reproduce --stack loopback-reference`: exit 0, "REPRODUCED: all 9 metrics
consistent"; 8 of 9 re-measured values inside the published intervals (barge-in p95 195 ms vs
published [180, 189], intervals overlapping).

## Spend

| What | USD |
| --- | --- |
| First `acme-chat-rules` run (luna customers and judges; superseded) | 0.0296 |
| `acme-chat-luna` (stopped by exhausted credits; not published) | 0.0318 |
| `pipecat-assemblyai` (AssemblyAI streaming, metered at list price) | 0.0437 |
| Everything else (local models) | 0 |
| **Total** | **0.105** of the 1.20 cap |
