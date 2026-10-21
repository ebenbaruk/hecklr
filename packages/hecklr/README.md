# hecklr

Meta-package, command-line interface and Python API for [Hecklr](../../README.md).

```bash
pip install hecklr            # chat + email
pip install "hecklr[voice]"   # adds the voice engine
pip install "hecklr[all]"     # everything
```

## Quickstart

```bash
hecklr init            # writes hecklr.yaml, a world, 5 scenarios, 3 personas (and a starter agent)
hecklr doctor          # Python, extras, keys (names only), Ollama, ports, agent reachability
hecklr run             # runs suite `ci`, prints pass rates with confidence intervals
```

`hecklr init --agent-url http://localhost:8000/chat` probes the URL: an OpenAI-compatible server
(`/v1/models`) gets the `openai-compatible` adapter, anything else the `http` adapter, with the
reply field detected from one test message. Without a URL, an in-process starter agent is written.

`hecklr run` flags: `--suite`, `--channels`, `--trials`, `--budget-usd`, `--seed`, `--adaptive`,
`--baseline RUN_ID`, `--output-dir`; `-v`/`-vv` show harness logs. Exit codes: 0 completed,
3 the run failed as a whole, 4 budget refused or exhausted, 78 configuration error.

`hecklr run` prints the run's metrics with intervals (compared with `--baseline` when given) and
every scenario's pass rate with its Wilson interval, and writes `report.html` into the run
directory.

## Comparing and gating

```bash
hecklr compare baseline                 # latest run vs the run before it (or its --baseline)
hecklr compare RUN_A RUN_B -f md        # md | html | json | junit | terminal (default)
hecklr gate --suite ci                  # exit 0 pass, 1 regression, 2 inconclusive, 3 harness error
hecklr report [RUN] -f html -f junit --open
hecklr calibrate --aa                   # same agent twice + simulated false-alarm rate (SC5)
hecklr calibrate --judges --labels labels.jsonl --run RUN   # Cohen's kappa per rubric (SC7)
```

Runs are named by id or `latest`; `baseline` is the candidate's recorded baseline, else the
newest earlier completed run of its suite. `hecklr gate` writes `report.md` (the PR comment),
`report.html`, `report.json` and `junit.xml` to `--out-dir` (default: the run directory);
`--missing-baseline pass` lets a first run without baseline pass. The GitHub Action
(`action/`) runs `hecklr run` and `hecklr gate` on pull requests. Decisions: ADR 0007c.

`hecklr replay logs.jsonl` turns real transcripts into replay scenarios under `scenarios/replay/`.

## Python

```python
import hecklr


@hecklr.agent(channel="chat")
async def reply(session: hecklr.Session, message: str) -> str:
    return await support_bot.answer(session.id, message)  # session.world_http_url for tools


results = hecklr.run("hecklr.yaml", suite="ci")  # or: await hecklr.run_async(...)
```

Reference the agent from `hecklr.yaml` with `chat: { type: python, entrypoint: "my_bot:reply" }`.

## pytest

```bash
pytest -p hecklr --hecklr-suite=ci [--hecklr-config=hecklr.yaml] [--hecklr-min-pass-rate=0.8]
```

One test per scenario. A scenario fails when every counted trial failed, or when the upper bound
of the 95% Wilson interval of its pass rate is below `--hecklr-min-pass-rate`. Run it without
`-n`: the suite runs once per process.

## Offline runs

`hecklr.testing.scripted_models()` provides a cooperative scripted customer and a rule-driven
judge for `fake/...` models, to check an agent's wiring in CI without any LLM; `hecklr run` uses
them for `fake/` models by default. `hecklr init` picks `openai/gpt-6-luna` when
`OPENAI_API_KEY` is set, Anthropic models with `ANTHROPIC_API_KEY`, a running Ollama next, and
`fake/` models (with a warning) otherwise; `hecklr doctor` shows which.
