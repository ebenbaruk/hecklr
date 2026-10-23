# hecklr-fix

Hecklr's fixer (spec, "Fixer", US-10): given one failure cluster, it asks a model for candidate
patches to the agent's editable files (system prompt, tool descriptions), runs each candidate
against the cluster's scenarios and then the guard suite, and keeps a patch only when it
significantly fixes the cluster without a significant regression anywhere. Otherwise it says
"no candidate beat the baseline". A human always merges. Decisions: ADR 0015a.

Part of [Hecklr](../../README.md). Owned by WS15.

## Usage

```bash
# 1. Something regressed: find what failed (or point at a mined cluster).
hecklr run --suite ci                                   # run 01M5...
# 2. Fix it. The fixer starts the agent itself, once per candidate, from a copy of the project.
hecklr fix --from-run latest --tag booking \
  --agent-cmd '{python} agent/serve.py --port {port}' \
  --candidates 5 --trials 3 --guard-suite ci --budget-usd 2 --pr
hecklr fix 01M5CLUSTERID... --agent-cmd '...'           # a cluster from `hecklr mine`
```

| Flag | Meaning |
| --- | --- |
| `CLUSTER` / `--from-run RUN` | The target: a mined cluster id, or what failed in a run (`latest` works). `--baseline RUN` keeps only scenarios whose pass rate dropped; `--tag`, `--scenario` narrow it |
| `--agent-cmd` | Starts the agent from the project copy: `{port}` (required), `{python}`, `{project}`; `{{`/`}}` for literal braces |
| `--candidates`, `--model` | How many patches, written by which model (default `models.judge`) |
| `--trials` | Trials per cluster scenario in the targeted pass (default 3) |
| `--guard-suite`, `--guard-trials` | The suite that must not regress (default `ci`) and its trials (default the suite's) |
| `--budget-usd`, `--seed` | One cap on Hecklr's spend for the whole session; one seed for every run |
| `--pr` (`--draft`) | Commit the winner on `hecklr-fix/<fix id>` in a temporary worktree, push, `gh pr create` |

Exit codes: 0 a validated patch; 1 no candidate beat the baseline, or nothing to fix; 3 the agent
or a run failed; 4 budget; 78 configuration.

## What it guarantees

- **Only `agent.editable` changes.** The model proposes `find`/`replace` edits; Hecklr applies
  them and writes the diff. An edit to any other file, or an `editable` entry that is
  `hecklr.yaml`, the world, a scenario or a persona, is refused. The user's files are never
  written: every pass runs in a copy, and `--pr` commits from a separate worktree.
- **Validated, with intervals.** Targeted pass: kept on an `improvement` verdict on the cluster's
  scenarios. Guard pass: dropped on a `regression` of the suite or any tag (Benjamini-Hochberg
  across tags). Both pair baseline and candidate trial by trial (same seed); every Δ is reported
  with its 95% CI and p-value.
- **Honest outcomes.** `fixed`, `no_candidate_beat_baseline`, `baseline_passes` or
  `budget_exhausted`, with every candidate's status and why proposals were dropped.

## Output

`<output_dir>/fix/<fix id>/`: `report.md` (the comparison report, also printed and used as the PR
body), `winner.diff` (`patch -p1 -d <project> < winner.diff`), `patches.jsonl` (frozen `Patch`
records), `cluster.json`, `summary.json`, `logs/<pass>.log`. Runs live in `<output_dir>/fix/runs`
with their own response cache, apart from the project's runs.

## Python API

```python
from hecklr_fix import (
    AgentCommand,
    Fixer,
    FixSettings,
    load_editable,
    target_from_run,
    render_markdown,
    write_session,
)

fixer = Fixer(
    config_path=...,
    editable=load_editable(config, config_path),
    target=target,
    settings=FixSettings(model="openai/gpt-6-luna", guard_suite="ci", seed=0, stats=config.stats),
    llm=llm,
    runner=suite_runner,
    launcher=AgentCommand("{python} serve.py --port {port}"),
    session_dir=...,
    runs_output_dir=...,
)
result = await fixer.run()
```

`runner` is any `SuiteRunner` (the CLI's runs a suite in process like `hecklr run`); `launcher`
any `AgentLauncher`. Tests use `FakeLLM` and fake runners (`tests/fixer/test_fixer.py`); the end-to-end
test (`packages/hecklr/tests/test_cli_fix.py`) fixes F1 on the reference agent for free.
