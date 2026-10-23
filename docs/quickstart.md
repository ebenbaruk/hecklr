# Quickstart

From nothing to a graded run with confidence intervals and an HTML report, in three commands and
well under five minutes (success criterion SC1, checked by an executed test of this page in CI).

## Three commands

<!-- docs-test: quickstart -->
```bash
pip install hecklr
hecklr init --yes      # hecklr.yaml, a world, 5 scenarios, 3 personas and a starter agent
hecklr run             # 5 scenarios x 3 trials, pass rates with 95% CIs, report.html
```

!!! note "Before the first PyPI release"
    Hecklr is not on PyPI yet. Until it is, install it from a checkout:

    ```bash
    git clone https://github.com/ebenbaruk/hecklr && cd hecklr
    uv sync --all-packages --all-extras
    source .venv/bin/activate     # puts `hecklr` on your PATH
    cd .. && mkdir my-agent-tests && cd my-agent-tests
    ```

    Then run `hecklr init --yes` and `hecklr run` as above. Extras will be `hecklr[voice]`,
    `hecklr[local-voice]`, `hecklr[mine]`, `hecklr[ui]` and `hecklr[all]`.

`hecklr init` writes a starter project into the current directory:

```text
hecklr.yaml                  agent, world, suites, models, statistics, budget
worlds/acme-starter.yaml     the stateful backend the agent's tools act on
scenarios/*.yaml             5 customer situations with success criteria
personas/*.yaml              3 simulated customers
hecklr_agent.py              a small in-process agent, so the first run works right away
```

It picks the models the simulated customers and the judges run on: `openai/gpt-6-luna` when
`OPENAI_API_KEY` is set, Anthropic models with `ANTHROPIC_API_KEY`, a local Ollama next, and
otherwise scripted `fake/` models that test the wiring (not the agent) for free. `hecklr doctor`
shows what it found.

`hecklr run` places every conversation, grades it and prints the run:

```text
Hecklr run 01M5K2CN92CTR65YZREE56HSCD · agent qs · suite ci
                   Run metrics (95% CI)
┏━━━━━━━━━━━━━━━━━━━━━━┳━━━━━━━━━━━━━━━━━━━━━━━━━━━━┳━━━━┓
┃ Metric               ┃                      Value ┃  n ┃
┡━━━━━━━━━━━━━━━━━━━━━━╇━━━━━━━━━━━━━━━━━━━━━━━━━━━━╇━━━━┩
│ pass_rate            │     100.0% [56.6%, 100.0%] │  5 │
│ pass^3               │     100.0% [56.6%, 100.0%] │  5 │
│ pass@3               │     100.0% [56.6%, 100.0%] │  5 │
│ response_latency_p50 │          0 ms [0 ms, 1 ms] │ 15 │
│ response_latency_p95 │       19 ms [17 ms, 64 ms] │ 15 │
│ cost_usd_per_trial   │ $0.0000 [$0.0000, $0.0000] │ 15 │
└──────────────────────┴────────────────────────────┴────┘
15 of 15 trials analyzed · 0 harness errors · 0 undecided (0 grade errors) · 0 agent errors · 0 timeouts
Simulator contradiction rate 0.0% [0.0%, 20.4%]
...
Run 01M5K2CN92CTR65YZREE56HSCD (completed)
Report: .hecklr/runs/01M5K2CN92CTR65YZREE56HSCD/report.html
```

Note the interval: five scenarios that all passed still give a suite pass rate of
**100% [56.6%, 100%]**. Five passing scenarios are weak evidence, and Hecklr says so instead of
printing "100%". See [Statistics](concepts/statistics.md).

Open the report with `hecklr report latest --open`: one self-contained HTML file with the run
overview, every scenario's interval, and a viewer for each trial (transcript, tool calls, world
diff, judge rationale, and for voice the audio with its waveform and turn markers).

## Point it at your agent

<!-- docs-test: agent-url -->
```bash
hecklr init --yes --agent-url http://localhost:8000/chat --force
hecklr run
```

`--agent-url` probes the endpoint: an OpenAI-compatible server (`/v1/models`) gets the
`openai-compatible` adapter, anything else the `http` adapter, with the JSON field holding the
reply detected from one test message. Without `--yes`, `hecklr init` asks you to confirm the
simulator and judge models it picked. Every other transport (streaming SSE, WebSocket, in-process
Python, WebRTC, LiveKit, Pipecat, Twilio-style media streams, SMTP/IMAP, webhooks) is a few lines
of `hecklr.yaml`: see [Connect your agent](adapters.md).

Your agent's tools should call the trial's **world sandbox** rather than your real backend, so
state assertions can check what the agent actually did. Every request carries the sandbox URL in
the `X-Hecklr-World-Url` header; see [Scenarios, personas and worlds](concepts/scenarios.md#worlds).

With a model key set, re-running `hecklr init --force` rewrites the starter project with real
models (or edit `models:` in `hecklr.yaml`); then replace the starter scenarios with what your
customers actually ask:

```bash
export OPENAI_API_KEY=...          # or ANTHROPIC_API_KEY, or a local Ollama
hecklr init --force --agent-url http://localhost:8000/chat
hecklr doctor                      # keys (names only), extras, ports, agent reachability
hecklr run --budget-usd 1          # a hard cap on what this run may spend
```

## Try the reference agent

The repository ships `acme-maintenance`, the support agent of a fictional maintenance company,
with 64 guided scenarios, 33 scripted ones, and twelve seeded faults. Its deterministic rules
brain and the scripted `smoke` suite cost nothing:

<!-- docs-test: reference-agent -->
```bash
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000
# in a second terminal:
cd examples/acme-maintenance
uv run hecklr run --suite smoke                 # the clean agent: 33/33
```

Now restart the agent with fault F1, a one-line change to its system prompt, and gate:

```bash
uv run python examples/acme-maintenance/agent/serve.py --brain rules --fault F1 --port 8000
uv run hecklr run --suite smoke
uv run hecklr gate --suite smoke                # exit 1: REGRESSION, 9 scenarios flipped
```

`demo/` in the repository records exactly this flow end to end; its numbers are in
[the demo results](https://github.com/ebenbaruk/hecklr/blob/main/demo/RESULTS.md).

## Next

- [Concepts](concepts/index.md): scenarios, personas, worlds, trials, graders and the statistics.
- [CI and the GitHub Action](guides/ci.md): fail a pull request only on a significant regression.
- [Voice](guides/voice.md): real audio calls, measured from the caller's side.
