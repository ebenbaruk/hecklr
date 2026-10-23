# Runs and trials

A **run** is one execution of a suite against one version of your agent. A **trial** is one
conversation in it: one scenario × one persona × one channel × one trial index. A suite with 20
scenarios, 2 personas each, on chat and voice, with `trials: 3`, is 20 × 2 × 2 × 3 = 240 trials.

## Suites

Suites are named selections in `hecklr.yaml`:

```yaml title="hecklr.yaml"
version: 1
agent:
  name: acme-support
  adapters:
    chat: { type: http, url: "http://localhost:8000/chat/stream", stream: sse }
    voice: { type: websocket, url: "ws://localhost:8765/", protocol: pcm }
  editable: [prompts/system.md, tools/descriptions.yaml]   # what the fixer may change
world: worlds/acme-maintenance.yaml
suites:
  ci:      { scenarios: "scenarios/**/*.yaml", tags: [core], trials: 3, channels: [chat] }
  nightly: { scenarios: "scenarios/**/*.yaml", trials: 10, channels: [chat, voice], adaptive: true }
  redteam: { scenarios: "redteam/*.yaml", trials: 5 }
models:
  simulator: openai/gpt-6-luna
  judge: openai/gpt-6-luna
stats: { alpha: 0.05, min_effect: 0.03 }
budget: { usd: 25, concurrency: 32 }
baseline: { store: local, branch: main }
seed: 20261021
```

`hecklr run --suite nightly` runs one; `--trials`, `--channels`, `--budget-usd` and `--seed`
override the file for one run.

## What a trial records

Every trial is a stream of **events**, each stamped with `t_ms`, integer milliseconds on the
harness's monotonic clock since the trial started:

| Event | Carries |
| --- | --- |
| `utterance` | who spoke, the text, and for the agent its response latency (`latency_ms`) |
| `audio_span` | a stretch of speech on the recording (voice) |
| `tool_call`, `tool_result` | every world tool call with its arguments and result |
| `interrupt` | a barge-in or backchannel, and how long the agent took to stop (`reaction_ms`) |
| `silence` | a turn gap, a caller hesitation, or a re-engagement silence (voice) |
| `email` | every email with its headers and attachments |
| `handoff`, `error` | the agent handing over to a human; an agent or harness error |

A trial ends with a status: `ok`, `agent_error` (your agent failed: counted as a failure),
`timeout`, or `harness_error`. Harness errors (Hecklr's own fault, or a provider outage) are
retried with exponential backoff and, after 3 attempts, excluded from the statistics with a
visible count. They are never blamed on your agent.

## Where results go

```text
.hecklr/
├── runs/<run id>/
│   ├── results.duckdb        runs, trials, events and grades, queryable with SQL
│   ├── trials.parquet        the same tables as Parquet, for pandas, polars or DuckDB
│   ├── events.parquet
│   ├── grades.parquet
│   ├── run.json              the run record: config hash, seed, agent, git sha, status
│   ├── report.html           one self-contained file
│   └── audio/<trial id>/     caller, agent and mix WAVs of every voice trial
└── cache/                    LLM and TTS response cache
```

Runs are named by id or `latest` in every command. `hecklr report RUN -f html -f md -f json -f
junit` writes any format later; `hecklr ui` browses them all.

## Reproducibility, cost and concurrency

- **Seeds.** The run's root `seed` derives a seed for every trial; the simulator, the turn-taking
  controller, voice impairments and tool faults all draw from it. Statistics take their own seed
  (`stats.seed`, the run seed by default).
- **Response cache.** Every simulator and judge call is keyed by (model, prompt hash, seed).
  Re-running an unchanged trial costs zero tokens; a changed agent reply changes the next prompt,
  so only what changed is paid for.
- **Budget.** Before starting, Hecklr estimates the run's worst-case cost and refuses to start if it
  exceeds `budget.usd` (or `--budget-usd`). During the run the budget is a hard cap: when it is
  reached the run stops cleanly and says so. A paid model with no known price is refused rather
  than counted as free; declare missing prices under `models.prices`.
- **Concurrency.** Trials run on one async task group, capped by `budget.concurrency` and by
  per-provider rate limits. Audio processing runs in a process pool, never on the event loop, so
  voice timing stays accurate under load.

Your agent's own model spend happens in your agent's process: Hecklr's budget covers the simulated
customers and the judges.

## Adaptive trials

`hecklr run --adaptive --baseline <run id>` (or `adaptive: true` on a suite) first runs the planned
trials, then spends up to 50% more on the scenarios whose outcome is uncertain or flipped against
the baseline, with a group-sequential design so the extra looks do not inflate false alarms. See
[Statistics](statistics.md#adaptive-trials).
