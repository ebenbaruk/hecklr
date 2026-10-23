# 0015a. Fixer: agent reload contract, patch format and the two statistical passes

Date: 2026-10-23 · Status: accepted (architect review below, 2026-10-23) · Workstream: WS15

## Context

`hecklr fix` (spec, "Fixer", US-10) generates N candidate patches to the files `agent.editable`
names, keeps those that significantly improve the failure cluster's scenarios (targeted pass) and
drops those that significantly regress the suite on any tag (guard pass). The spec leaves open:
how a candidate's files reach the agent under test (Hecklr does not build agents and the agent
reads its prompt at start-up, in its own process), how patches are written, what the input looks
like when the failure comes from a run rather than the miner, and which statistical unit decides
on a cluster of a handful of scenarios. With 5 or fewer scenarios a scenario-paired sign-flip test
can never reach α = 0.05 (ADR 0005b), and most clusters are that small.

## Decision

- **Agent reload contract.** The user gives a command template, `--agent-cmd`, e.g.
  `'{python} agent/serve.py --brain rules --port {port}'`. For every pass the fixer copies the
  directory holding `hecklr.yaml` to `<output_dir>/fix/<fix id>/work/<pass>/` (without `.git`,
  the output dir, virtualenvs and caches), writes the candidate's editable files into the copy,
  rewrites the port of every adapter `url` to a free port, and runs the template there with
  `{port}`, `{python}` (Hecklr's interpreter) and `{project}` (the copy) filled in. No shell is
  used (`shlex.split`). The agent is ready when its port accepts a TCP connection (default 60 s);
  it is stopped with SIGTERM, then SIGKILL after 10 s, sent to its own process group, so a wrapper
  such as `uv run` and the server it starts both stop and nothing else is touched. Passes run one
  after another. Agents reached only in process (`python` adapter) or at a remote URL cannot be
  reloaded and are refused. The template lives on the command line because `hecklr.yaml` is
  frozen with `extra="forbid"`; RFC 0015a proposes `agent.command` for it.
- **Patch format.** The model answers JSON `{rationale, edits: [{file, find, replace}]}`; Hecklr
  applies each edit only if `file` is editable and `find` occurs exactly once, and writes the
  unified diff itself (`difflib`, `git apply`-compatible). A proposal that edits anything else,
  does not apply, changes nothing, repeats an earlier one or is not valid JSON is dropped before
  any run, with its reason in the report. Entries of `editable` that are `hecklr.yaml`, the world,
  or any file a suite's scenario or persona glob (or the persona library) selects are refused
  outright, so no patch can pass by changing the test. Rubrics and graders ship with `hecklr-grade`
  and are outside the project. The user's files are never written; `--pr` commits in a temporary
  `git worktree` from `HEAD` and refuses when the editable files have uncommitted changes.
- **Targets.** A mined cluster (`hecklr fix CLUSTER`) runs the scenarios proposed from it that are
  not rejected, read through `hecklr_mine` (`load_clusters`, `load_queue`: the ADR 0013a files,
  newest record wins), so the fixer never re-implements the miner's file layout. `--from-run RUN` builds a
  `FailureCluster` (`source: run`) from the run's scenarios with a failed trial, or with
  `--baseline RUN` those whose pass rate dropped, narrowed by `--tag` / `--scenario`. Either way the
  scenarios are written into the copy as the suite `hecklr-fix-targeted`. The generator sees the
  failing trials of the fixer's own targeted baseline (transcripts, failing criteria and judge
  rationales, round-robin over scenarios, 6 by default), the cluster label and the editable files.
  If that baseline passes every trial, the outcome is "nothing to fix" and no model is called.
- **Statistical unit: the trial cell.** Both passes compare the current agent with the candidate
  on a fixed set of scenarios: "does it fix these", "does it break this suite". Every run of a
  session uses one seed, so cell (scenario, persona, channel, trial index) has the same simulator
  seed on both sides, and the sign-flip test pairs cells. Under "the patch changes nothing" each
  paired difference is symmetric, so the test is valid; it conditions on the scenarios instead of
  generalizing over them as `hecklr gate` does. Verdicts use `hecklr_stats.compare_runs` (paired
  cluster bootstrap CI, sign-flip p, the spec's verdict rule with the project's α and δ).
  - Targeted pass: kept when the whole-set verdict is `improvement`.
  - Guard pass (`--guard-suite`, default `ci`, the suite the gate runs): dropped when the suite or
    any tag has a `regression` verdict. Outcomes carry tags only, so the Benjamini-Hochberg family
    is exactly the tags (the spec's "any tag"). The guard baseline runs once per session.
  - Winner: the accepted candidate with the largest targeted Δ, then the largest guard Δ, then
    the lowest index.
- **Budget and outcomes.** One cap (`--budget-usd`, default `budget.usd`) covers generation and
  every run; each run gets what is left. Outcomes: `fixed` (exit 0), `no_candidate_beat_baseline`
  and `baseline_passes` (exit 1), `budget_exhausted` (exit 4). A winner found before the budget ran
  out is still reported, with a note. The agent's own model spend is outside Hecklr's meter, as in
  `hecklr run`; the agent's own cap (e.g. acme's `--budget-usd`) goes in `--agent-cmd`.
- **Artifacts.** `<output_dir>/fix/<fix id>/`: `cluster.json`, `patches.jsonl` (frozen `Patch`
  records with both passes' `Comparison`s), `report.md`, `winner.diff`, `logs/`, and
  `summary.json` written last. Runs and the response cache go to `<output_dir>/fix/runs` and
  `<output_dir>/fix/cache`, shared by sessions (a re-run hits the cache) and apart from the
  project's runs, so `hecklr gate` and `latest` never pick a candidate's run.

## Consequences

- Any agent that can start from its project directory on a given port can be fixed; the reference
  agent's rules brain reads its prompt, so the end-to-end test (F1 fixed, planted bad patch
  rejected) is free and deterministic.
- Cell pairing has power on small clusters but treats trials of one scenario as separate units:
  a patch that helps on these scenarios may not generalize to unseen ones. The guard pass and the
  human review of the PR are the checks on that; the gate remains scenario-paired. On a
  deterministic agent, replicated trials add cells without adding information about noise
  (there is none); use 3 trials so a tag of 6 scenarios can reach significance after BH.
- Sessions are sequential: N candidates cost N + 1 targeted runs and up to N + 1 guard runs.
- Copying the project per pass is cheap for an agent project but not for a monorepo root; then
  put `hecklr.yaml` next to the agent, not at the repository root.

## Acceptance review

Architect review (Wave 4 integration, 2026-10-23), checked against SPEC.md "Fixer
(`hecklr-fix`)", "Comparing baseline and candidate", "Configuration (`hecklr.yaml`)" and US-10:

- **Spec steps 1-5 are all met.** Input is one cluster plus `agent.editable`; N candidates
  (default 5) from an LLM that sees failing transcripts, judge rationales and the current files;
  a targeted pass that keeps only significant improvements; a guard pass that drops any
  significant regression on any tag; the winning diff, its report and `--pr` via `gh`.
- **Guardrails are stricter than the spec, not looser.** Editable entries that define the test
  (config, world, any scenario or persona a suite can select) are refused even when listed;
  rubrics and graders are outside the project; "no candidate beat baseline" is an outcome with
  its own exit code; `--pr` opens a draft, so a human merges.
- **Cell pairing is a sound deviation for this question.** The spec pairs by scenario because a
  gate generalizes over scenarios. The fixer asks a narrower question ("does it fix *these*
  scenarios, does it break *this* suite") on clusters too small for scenario pairing ever to
  reach α (ADR 0005b). With one seed per session both arms share each cell's simulator seed, so
  under "the patch changes nothing" the paired differences are exchangeable and the sign-flip
  test is valid. The verdict rule, α, δ and BH are the spec's. The limitation (no claim about
  unseen scenarios) is stated in Consequences, and the gate remains scenario-paired.
- **"Full suite" read as `--guard-suite`, default `ci`.** The spec says the guard pass runs "the
  full suite"; the gate's suite is the one a regression would fail in CI, so it is the right
  default, and `--guard-suite nightly` runs a larger one. Accepted as an interpretation.
- **Configuration.** The start command is needed because Hecklr does not build agents and the
  agent reads its prompt at start-up. `AgentConfig` is frozen (`extra="forbid"`) and
  `hecklr-schemas` is frozen after Wave 0, so `--agent-cmd` is the accepted mechanism for v1.
  RFC 0015a's `agent.command` field is **deferred to v1.1**; no schema change is made now.

Accepted. Follow-up for v1.1: RFC 0015a.
