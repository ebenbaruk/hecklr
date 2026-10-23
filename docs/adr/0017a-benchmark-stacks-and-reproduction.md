# 0017a. The benchmark: one fixed suite, stacks as processes, reproduction by overlapping intervals

Date: 2026-10-23 · Status: proposed · Workstream: WS17 (Wave 4)

## Context

SC10 asks for a benchmark of at least six stacks and US-12 for `hecklr bench` to reproduce a
leaderboard row. The spec leaves open which stacks (it needs API access and budget per provider)
and what "reproduce" means for measurements that are themselves samples. The build had USD 1.20
of live budget for every stack together, one laptop shared with other builders, and the
reference agents of Waves 1-2 (acme chat and voice, the loopback agent) to build on.

## Decision

- **A stack is a process.** `bench/stacks/<id>.yaml` gives the command that starts the agent,
  how to tell it is ready (an HTTP 200 or a log line), the `hecklr.yaml` adapter entry, an
  optional `spend_url` and its caps. Placeholders (`{python}`, `{repo}`, `{port}`, `{port1}`,
  `{port2}`, `{seed}`, `{agent_budget_usd}`) are filled per run with free ports. The runner
  signals only the process it started; an agent that owns other resources (the LiveKit stack's
  Docker container) cleans them up on SIGTERM.
- **One suite, three tracks.** `bench/bench.yaml` pins the version, seed, simulator and judge
  models and the trials per track; `bench/suite/` holds a frozen copy of the world, 8 guided chat
  scenarios, 9 scripted voice scenarios and 3 calibration calls. A SHA-256 of the manifest and the
  suite is recorded in every row, and `reproduce` refuses a row whose digest differs.
- **Every voice stack runs the same brain** (acme rules): rows differ only by transport, VAD,
  turn detection, ASR and TTS, which is what a voice-stack benchmark compares. Timing is reported,
  never folded into task success.
- **Concurrency 1.** Stacks never compete with themselves for the CPU, and the agent's own spend
  can be read from its `spend_url` after each conversation, which makes cost per conversation a
  per-trial quantity with a bootstrap interval. The 1-minute load average at the start and end of
  a run is recorded, because local speech models slow down on a busy machine.
- **Metrics** are `Estimate`s from the existing statistics: task success and per-rubric judge
  pass rates (scenarios weighted equally, cluster bootstrap or Wilson on the scenario count,
  ADR 0005c), latency and barge-in quantiles bootstrapped over conversations, per-call voice
  metrics, and cost per conversation.
- **Reproduction = overlapping 95% intervals.** A re-measurement is a sample too: its point
  estimate falls outside the published interval far more often than 5% of the time even when
  nothing changed (about 17% for equal sample sizes). `reproduce` therefore requires each metric's
  new interval to overlap the published one (exit 1 otherwise) and also reports whether the new
  point lies inside the published interval. Timing intervals (latency, barge-in) are widened by
  20 ms on each side first, the harness's own timing accuracy (SC4): a bootstrap interval covers
  sampling noise within one run, not the milliseconds scheduling moves between runs, and an agent
  answering in 3-5 ms otherwise fails to reproduce on an idle rerun (seen in the test suite).
- **Cloud speech is metered by the agent at list price** (AssemblyAI streaming session time,
  OpenAI `gpt-4o-mini-tts` minutes of speech), since neither API returns a cost; the agent refuses
  new calls past its cap.
- **The site is static** (`bench/site/src`, plain HTML/CSS/JS), rendered by `hecklr bench site`
  with the rows as `data.js` so `dist/index.html` opens from disk. Nothing is deployed.
- The runner lives in `hecklr.commands.bench` (a package, not one module): the CLI, the spec
  files, the process, the metrics and the site are five small modules.

## Consequences

- Adding a stack is one YAML file (plus an agent script when no existing agent fits); it is
  measured under the same seed, suite and caps as the others.
- Rows measured on a loaded machine are marked by their load average rather than silently
  slower; publishing numbers for a launch post should re-run the voice rows on an idle machine.
- Overlap is a lenient criterion: it can miss a small real drift. It never fails an honest
  re-run, which is what a reproduction check must not do; a stricter paired comparison would need
  the trial-level data of both runs, which `hecklr compare` already offers.
- Pipecat 1.13's `WhisperSTTService` cannot be imported on macOS without the optional
  `mlx-whisper` package, so the Pipecat stack uses a 30-line `SegmentedSTTService` over
  faster-whisper that decodes the way Pipecat's service does.
