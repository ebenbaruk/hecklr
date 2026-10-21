# 0007c. compare, gate, report, calibrate and the GitHub Action

Date: 2026-10-21 · Status: proposed · Workstream: WS7c

## Context

`hecklr-report` (ADR 0007b) turns stored runs into an `Analysis` and an exit code, but nothing
named which runs to compare, what `hecklr gate` does without a baseline, how the A/A self-test
runs, or how CI finds the baseline and posts one comment. The spec leaves these open; ADR 0007b
asked WS7a to handle the "no baseline yet" case explicitly.

## Decision

- **Run references.** Commands take a run id or an alias. `latest` is the newest *finished* run
  (completed, budget exceeded, cancelled or failed; optionally of `--suite`), so a gate after a
  crashed run exits 3 instead of gating an older run. `baseline` is relative to the candidate:
  the baseline it recorded (`hecklr run --baseline`), else the newest *completed* run of the same
  suite created before it.
- **`hecklr gate` only compares.** It does not run the suite; CI runs `hecklr run` then
  `hecklr gate` (the Action does both). It writes all four report files, prints the Markdown
  comment, and exits with `gate_exit_code`. Every failure to load the config or find a run exits
  3, never 78: the gate's contract is 0-3. With no baseline at all, `--missing-baseline error`
  (the CLI default) exits 3; `pass` (the Action's default) reports the run alone and passes when
  the candidate is sound, so a repository's first PR is not red.
- **`hecklr run` summary** is `render_terminal` (compared with the recorded baseline) plus a
  table of every scenario's Wilson interval (US-01: `render_terminal` lists only failures and
  flips), and it writes `report.html` into the run directory.
- **A/A self-test (`calibrate --aa`).** Two runs of the same agent with root seeds `s` and `s+1`:
  the same seed would replay the LLM response cache and prove nothing. Then 200 simulated A/A
  comparisons at the suite's shape (`hecklr_stats.verdict_rate`) report the false-alarm rate, and
  the power for a 10-point drop is shown. It fails (exit 1) when the real comparison is a
  regression or the simulated rate exceeds α.
- **Judge calibration (`calibrate --judges`)** reads human labels as JSONL of `HumanLabel` (the
  UI labeling queue, WS16, will write them) and groups the runs' judge grades by rubric, version
  and judge model; any rubric below κ 0.70 fails the self-test.
- **`fake/` models default to `hecklr.testing.scripted_models`** when no providers are injected:
  a project without keys or Ollama (`hecklr init` writes `fake/` models with a warning) runs end
  to end as a wiring smoke test instead of failing every trial.
- **Model defaults** for `hecklr init`: OpenAI `gpt-6-luna` for both roles when `OPENAI_API_KEY`
  is set (its price is written into `hecklr.yaml`), Anthropic when only `ANTHROPIC_API_KEY` is,
  then a running Ollama, then `fake/`.
- **Action.** A composite action whose logic lives in a hidden `hecklr github` command group
  (typed and unit-tested, calling the runner's `gh` CLI): `fetch-baseline` downloads the newest
  unexpired `hecklr-baseline-<suite>` artifact uploaded from the baseline branch; `comment`
  creates or updates the comment whose first line is `<!-- hecklr-gate:<suite> -->`;
  `latest-run --since <start of the run step>` names the run this job recorded: the Action gates
  that run (`--candidate`), never `latest`, which after a run that stopped before recording would
  be the fetched baseline. Any non-zero `hecklr run` exit, or no recorded run, makes the Action
  exit 3; `gate`, `compare` and `calibrate --aa` refuse to compare a run with itself (PR #22
  review).
  Model providers are checked before the plan is printed, so a missing key exits 78 with no run
  id shown (PR #11 review).

## Consequences

- Locally, `hecklr run` then `hecklr gate` compares with the previous completed run of the
  suite, which is what "did my change break it" means on a laptop.
- The Action needs `actions: read` and `pull-requests: write`. Baselines expire with artifact
  retention (90 days); after that the next PR has no baseline and passes with a notice.
- The self-test workflow proves the Action on every PR touching it, for free, on the reference
  agent's rules brain (clean exits 0, F3 exits 1).
