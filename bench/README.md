# Hecklr benchmark

Support-agent stacks measured on equal footing: the same scenarios, simulated customers, seed,
models and trial counts against every stack, and every number with its 95% confidence interval.
Built in WS17 (spec: SC10, US-12; decisions: [ADR 0017a](../docs/adr/0017a-benchmark-stacks-and-reproduction.md)).

```text
bench.yaml              version, seed, models, trials per track (pinned)
suite/                  the frozen suite: world, personas, scenarios/{chat,voice,calibration}
stacks/<id>.yaml        one stack: how to start it, reach it, and what it is made of
stacks/agents/          agent scripts for stacks no example agent covers (Pipecat, LiveKit)
results/<id>.json       published leaderboard rows (one per stack)
site/src/               the static leaderboard (plain HTML, CSS, JS)
site/dist/              built by `hecklr bench site` (git-ignored, never deployed)
```

## Commands

```bash
uv sync --all-packages --all-extras
uv run hecklr bench list                                   # stacks, what they need, max cost
uv run hecklr bench run --stack acme-voice-local
uv run --env-file .env hecklr bench run --all --budget-usd 0.50   # cloud stacks need their keys
uv run hecklr bench reproduce --stack acme-voice-local
uv run hecklr bench site && open bench/site/dist/index.html
```

`run` starts the stack's agent, writes a `hecklr.yaml` for it over a copy of the suite (under
`--work-dir`, default `.hecklr/bench/<stack>/<time>/`: results store, response cache, agent log),
runs the track one conversation at a time and writes `results/<stack>.json`. `--budget-usd`
refuses to start when the chosen stacks' caps add up to more. Exit codes: 0 measured, 3 a stack
could not be measured (the others still run), 4 over budget, 78 a configuration error.

`reproduce` measures a stack again (fresh response cache) and compares every metric with the
published row: it passes (exit 0) when every pair of 95% intervals overlaps, and also says
whether each new value falls inside the published interval. It refuses a row measured on another
benchmark version or suite digest (exit 78).

## Tracks

| Track | Scenarios | Callers | Graded by |
| --- | --- | --- | --- |
| `chat` | 8 acme tasks: book, reschedule, cancel, invoice, fee question, gas emergency, refund, a data prober | guided (qwen3 4B on local Ollama), 2 trials each | tool calls, world state, safety graders, judges (`resolved`, `no_hallucination`, `handoff_appropriate`) |
| `voice` | 9 calls: 3 latency (steady caller), 3 barge-in (interrupts every reply), 3 hesitation (450-550 ms pauses) | scripted, Kokoro voices, 2 trials each | world state, tools not called after a retraction, `resolved` judge; Hecklr hears the agent with faster-whisper `base.en` |
| `calibration` | 3 calls against an agent with known timing | scripted, 3 trials each | a timing criterion; the row is a ruler, not a contestant |

## Stacks

| Stack | Track | Made of | Why it is here |
| --- | --- | --- | --- |
| `acme-chat-rules` | chat | HTTP+SSE, keyword rules brain | the free, exactly repeatable floor |
| `acme-chat-luna` | chat | HTTP+SSE, gpt-6-luna tool loop | a small hosted model as most teams ship it (needs OpenAI credits) |
| `acme-chat-qwen3` | chat | HTTP+SSE, qwen3 4B on local Ollama | the fully local option |
| `acme-voice-local` | voice | PCM WebSocket, Silero, 800 ms endpointing, faster-whisper base.en, Kokoro | the hand-tuned local cascade (acme-voice reference) |
| `acme-voice-livekit` | voice | the same cascade behind a local LiveKit SFU (WebRTC, Opus) | isolates what the transport adds |
| `pipecat-local` | voice | stock Pipecat 1.13: SmallWebRTC, Silero + smart-turn v3, faster-whisper base.en, Kokoro | the common framework at its defaults, same models |
| `pipecat-assemblyai` | voice | the same Pipecat pipeline with AssemblyAI streaming ASR | one variable: streaming cloud ASR vs local batch ASR |
| `loopback-reference` | calibration | answers 600 ms after the caller stops, stops 150 ms after a barge-in | the ruler for the voice rows |

Hecklr's own models (guided customers, judges) are qwen3 4B on local Ollama with a 32k context,
so every row reproduces without an API key; create the model once with
`ollama create hecklr-bench-qwen3-4b -f bench/models/qwen3-4b-32k.Modelfile`. The `resolved`
judge then shares weights with the `acme-chat-qwen3` agent, a self-preference to keep in mind
when reading that row's judge score (its task success is graded by tools and world state).

Every voice stack runs the same acme rules brain, so voice rows differ only by their speech
stack. Each stack's file says what it is and why it is meaningful; the leaderboard shows both.

## Metrics

Every metric is a `hecklr_schemas.Estimate` (value, 95% interval, method, n):

- `task_success`: suite pass rate, scenarios weighted equally (cluster bootstrap; Wilson on the
  scenario count when every scenario is all-or-nothing, ADR 0005c);
- `judge:<rubric>`: share of judged conversations each rubric passed, same interval;
- `response_latency_p50` / `_p95`: chat time to first token, or voice end of caller speech to
  first agent audio, bootstrapped over conversations;
- voice: `barge_in_reaction_p50` / `_p95`, `false_interruptions_per_100_turns`,
  `dead_air_per_call`, `talk_over_s_per_min`;
- `cost_per_conversation_usd`: Hecklr's own model spend plus the agent's reported spend.

Rows also record the run id, seed, suite digest, library versions, git sha, spend split and the
machine's 1-minute load average at the start and end of the run.

## Adding a stack

Write `stacks/<id>.yaml` (see the existing files; placeholders `{python}`, `{repo}`, `{port}`,
`{port1}`, `{port2}`, `{seed}`, `{agent_budget_usd}`), plus an agent script under
`stacks/agents/` if no existing agent fits. An agent that spends money exposes `spent_usd` at its
`spend_url` and enforces `{agent_budget_usd}`. Then `hecklr bench run --stack <id>`.

## Results

> **Preliminary, not for publication.** These rows were measured on one shared laptop (Apple
> silicon, 10 cores) whose load average reached about 250 during the measurement window (other
> builds); each row records only its start and end 1-minute load, 4.0 to 20.7. Hecklr's customers
> and judges were qwen3 4B on local Ollama. The gpt-6-luna stack was measured later, on the same
> shared laptop (load 6.4 → 4.0). Re-measure every row on an idle machine before quoting a number.

See [RESULTS.md](RESULTS.md) for the measured rows, what was run and what it cost.
