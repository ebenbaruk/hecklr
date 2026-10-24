# Fixer

Given a failure cluster, the fixer proposes changes to the files you allow it to edit (your system
prompt, your tool descriptions), validates each candidate against your suite with the same
statistics as the gate, and hands you the winning diff, or tells you plainly that nothing beat the
baseline.

```bash
# A cluster from `hecklr mine` (needs the miner: pip install 'hecklr[mine]')
hecklr fix <cluster id> --agent-cmd '{python} agent/serve.py --port {port}'
# What failed in a run, or only what dropped against a baseline run, narrowed by tag
hecklr fix --from-run latest --baseline <run id> --tag booking --agent-cmd '{python} agent/serve.py --port {port}'
# Also open a pull request with gh
hecklr fix <cluster id> --agent-cmd '{python} agent/serve.py --port {port}' --pr --draft
```

## Starting your agent

Hecklr does not build your agent, and most agents read their prompt when they start. So the fixer
starts the agent itself, once per pass, from a copy of your project with the candidate's files
written in. `--agent-cmd` says how: `{port}` (required) is a free port Hecklr also writes into the
copy's adapter URLs, `{python}` is Hecklr's interpreter and `{project}` the copy. The command is
split like a shell would split it before the placeholders are filled in, so a path with spaces
(`~/My Drive/...`) stays one argument; it runs without a shell, from the copy. The agent is ready when its port accepts a connection
(`--ready-timeout`, default 60 s); it is stopped with SIGTERM, then SIGKILL, sent to its own process
group. Agents reached only in process (a `python` adapter) or at a remote URL cannot be restarted
this way and are refused.

Keep `hecklr.yaml` next to the agent rather than at a large repository root: the project directory
is copied for every pass.

## How a fix is chosen

1. **Input:** one failure cluster plus the files listed under `agent.editable` in `hecklr.yaml`:

    ```yaml
    agent:
      editable: [prompts/system.md, tools/descriptions.yaml]
    ```

    The cluster is either one `hecklr mine` found (its proposed or approved scenarios run; rejected
    ones do not) or, with `--from-run`, the scenarios of a run with a failed trial. With
    `--baseline`, only scenarios whose pass rate dropped; `--tag` and `--scenario` narrow it.

2. **Generate** N candidate patches (`--candidates`, default 5) with an LLM (`--model`, default
   `models.judge`) that sees failing transcripts, the judges' rationales, the cluster's label and
   the current files. The model proposes find-and-replace edits; Hecklr applies them and writes the
   diff. A proposal that touches another file, does not apply, changes nothing or repeats an earlier
   one is dropped before anything runs, with the reason in the report.
3. **Targeted pass:** run the current agent and each candidate on the cluster's scenarios
   (`--trials`, default 3); keep a candidate only on a significant `improvement` verdict.
4. **Guard pass:** run the survivors on the guard suite (`--guard-suite`, default `ci`, the suite
   the gate runs; `--guard-trials`); drop any with a significant `regression` on the suite or on
   any tag (Benjamini-Hochberg across tags).
5. **Output** the winner (largest targeted Δ, then guard Δ) with its comparison report and, with
   `--pr`, a pull request.

Both passes pair the current agent and the candidate trial by trial: every run of a session uses
one seed (`--seed`), so each (scenario, persona, channel, trial) cell has the same simulated
customer on both sides. Every Δ is reported with its 95% interval and p-value. This answers "does
it fix *these* scenarios, does it break *this* suite"; the gate's scenario-paired test is still
what decides whether it generalizes. The decisions are in
[ADR 0015a](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/0015a-fixer-agent-reload-and-validation.md).

## Outcomes and exit codes

| Exit | Outcome |
| --- | --- |
| 0 | `fixed`: a validated patch |
| 1 | `no_candidate_beat_baseline`, or `baseline_passes` (the current agent already passes every trial of the cluster; no model is called) |
| 3 | the agent or a run failed |
| 4 | the budget ran out (`--budget-usd`, default `budget.usd`, one cap for generation and every run; as for `hecklr run`, spend may reach it but not cross it, so `--budget-usd 0` runs free models only: `fake/`, `ollama/` or priced at 0 in `models.prices`); a winner found before that is still reported |
| 78 | configuration error (no `editable` files, no `--agent-cmd`, unknown suite or cluster, the miner not installed) |

## Output

Everything goes to `<output_dir>/fix/<fix id>/`: `report.md` (also printed, and the PR body),
`winner.diff` (apply with `patch -p1 -d <project> < winner.diff`), `patches.jsonl` (every
candidate with both comparisons), `cluster.json`, `summary.json` and the agent logs under `logs/`.
The fixer's runs and response cache live in `<output_dir>/fix/`, apart from your project's runs, so
`hecklr gate` and `latest` never pick a candidate's run.

With `--pr`, the winner is committed on `hecklr-fix/<fix id>` in a temporary git worktree from
`HEAD`, pushed, and opened with `gh pr create` (`--draft` for a draft). It refuses when the
editable files have uncommitted changes.

## Guardrails

- The fixer can never edit scenarios, graders, rubrics, or any file outside `editable`. Even if
  `editable` lists them, `hecklr.yaml`, the world and every scenario or persona a suite can select
  are refused, so no patch can pass by changing the test.
- Your files are never written: every pass runs in a copy.
- It reports "no candidate beat the baseline" honestly; it never lowers a bar to produce a win.
- Hecklr never merges: a human always does.
- The agent's own model spend is outside Hecklr's meter, as in `hecklr run`; put the agent's own
  cap in `--agent-cmd` if it has one.
