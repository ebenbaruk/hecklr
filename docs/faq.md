# FAQ

## Do I need an API key?

No, to try it: `hecklr init` falls back to a local Ollama model, or to scripted `fake/` models that
check your wiring for free. Scripted scenarios with deterministic graders never call a model at
all, and they catch real regressions (the reference agent's 33 scripted scenarios catch all six
chat faults). Guided customers and LLM judges need a model: `openai/gpt-6-luna` is the default
when `OPENAI_API_KEY` is set, Anthropic models with `ANTHROPIC_API_KEY`, or any local Ollama or
OpenAI-compatible server.

## How much does a run cost?

It depends on the models and the suite. As a measured reference, the chat reference agent's live
fault-matrix suite (56 scenarios × 3 trials, guided customers and judges on `gpt-6-luna`) cost about
$0.42 per run for Hecklr's side. Every run estimates its worst-case cost before starting, refuses
to start over budget (`--budget-usd`), and stops cleanly when the cap is reached. The response
cache makes re-running unchanged conversations free.

## Does my agent's model spend count against Hecklr's budget?

No. Your agent calls its own models in its own process; Hecklr's budget covers the simulated
customers and the judges.

## Do I have to change my agent?

No code changes are required to talk to it: adapters reach agents over the transports they already
serve. To get state assertions, point your agent's tool client at the trial's world sandbox in test
mode (its URL arrives in the `X-Hecklr-World-Url` header and equivalents on every transport). If
you cannot, `tool_mode: observed` reads tool calls from your OpenTelemetry spans instead.

## Why are my intervals so wide?

Because the evidence is thin, and Hecklr says so. Three passes out of three trials is
100% [43.9%, 100%]. Narrow intervals come from **more scenarios** more than from more trials,
because a scenario's trials are correlated. The reference suite is sized at 60 scenarios × 5 trials
to detect a 10-point drop with 80% power; see [Statistics](concepts/statistics.md).

## The gate said "inconclusive". What now?

The change might be real but the data cannot tell yet. The report says how many more trials per
scenario would likely decide it. Run with `--adaptive` to spend extra trials where they help most,
or set `gate.on_inconclusive: pass` if an undecided result should not block merges.

## Can the simulated customer be wrong instead of my agent?

Yes, and Hecklr guards against it: a consistency check regenerates simulator turns that contradict
the customer's brief, the contradiction rate is printed with an interval in every run, and A/A runs
(the same agent twice) measure how often noise alone would flag a regression. Scripted scenarios
remove the simulator entirely.

## How accurate are the voice latency numbers?

Within ±20 ms of the truth, checked in CI against a loopback agent with known timing; on the
networked loopback agent the worst error measured was 13 ms over 840 measurements. Timing is
measured in audio frames at the harness, from the caller's side. See [Voice](guides/voice.md).

## Can I place real phone calls?

Not by default: Hecklr is local-first and does not dial the phone network. It speaks the protocols
telephony stacks use (Twilio Media Streams, Pipecat, LiveKit, raw PCM over WebSocket). A SIP
adapter is on the [roadmap](roadmap.md).

## Where is my data?

On your machine: `.hecklr/runs/<run id>/` holds DuckDB, Parquet, WAV and HTML files. Nothing is
sent anywhere except to the model providers you configure. The miner scrubs personal data from
production logs before any model sees them (names need `--presidio` or `--llm-scrub`).

## Is it an agent framework or an observability platform?

Neither. Hecklr tests agents; it never builds them, and it ingests logs and traces without
replacing your observability dashboards.
