# bench (WS17) progress

Owner: WS17. Spec: `docs/SPEC.md` -> SC10, US-12, WS17 row, "Voice simulation engine > Metrics",
"Agent adapters", `hecklr bench`. Decisions: ADR 0017a.

## Plan

1. `bench/suite/`: a frozen copy of the world, 8 guided chat scenarios, 9 scripted voice
   scenarios (latency, barge-in, hesitation) and 3 calibration scenarios; `bench/bench.yaml`
   pins the benchmark version, the seed, the models and the trials per track.
2. `hecklr bench` (`packages/hecklr/src/hecklr/commands/bench/`): `list`, `run --stack`,
   `reproduce --stack`, `site`. A run starts the stack's agent process, writes a per-stack
   `hecklr.yaml` over the suite, runs it in process at concurrency 1, reads the agent's own
   spend per trial, and writes `bench/results/<stack>.json` (every metric an `Estimate`).
3. Stacks (`bench/stacks/*.yaml`, agents in `bench/stacks/agents/`): chat rules / luna / qwen3
   (local Ollama); voice: acme cascaded pipeline (local), the same pipeline over LiveKit, a
   Pipecat 1.13 pipeline (Silero + smart-turn + Whisper + Kokoro), the Pipecat pipeline with
   AssemblyAI streaming + OpenAI TTS; the loopback calibration agent as the timing reference.
4. Static leaderboard: `bench/site/src` rendered with the results into `bench/site/dist`.
5. Run every stack for real under the $1.20 cap, reproduce one, view the site in a browser.

## Done

(in progress)

## Open questions

- None yet.
