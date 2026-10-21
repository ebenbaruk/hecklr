# 0007a. Composition, grading ownership, `hecklr run` exit codes and the pytest plugin

Date: 2026-10-21 · Status: accepted · Workstream: WS7a

## Context

Wave 1 left every component behind a Protocol and nothing that wires them together. The
integrator found the seams did not meet: core's `GraderFactory` expected `CriterionGrader` pairs
while `hecklr_grade.build_graders` returns `BoundGrader`s, and grading logic existed twice (core's
executor looped graders itself, bypassing `grade_trial`'s phases, concurrency and error handling).
WS7a also needs exit codes for `hecklr run`, a way for tests to script models through the CLI, a
rule for when a scenario "fails" under pytest, and an adaptive pass, none of which the spec fixes.

## Decision

- **Grading has one owner: `hecklr-grade`.** `Collaborators.grade_trial` is a `TrialGrader`
  (`GradeContext -> Awaitable[Sequence[Grade]]`). The executor builds one context per trial
  (metered judge LLM, judge model, no criterion) and calls it; `hecklr.compose.make_trial_grader`
  builds each scenario's `BoundGrader`s once, before the run, and delegates to `grade_trial`.
  `CriterionGrader` and `GraderFactory` are removed. A grading callable that raises fails the run
  (a harness bug); budget semantics are unchanged (a trial whose grading crossed the budget is
  dropped). Supersedes the `CriterionGrader` part of ADR 0001a.
- **Composition in two steps.** `prepare_run` loads (world seed files inlined with
  `hecklr_world.load_world`), resolves the suite, builds graders and drivers, and plans; every
  configuration error surfaces there. `execute_run` opens the model stack, the sandbox and the
  store, runs, and returns `RunResults`. Voice and email suites fail in `prepare_run` with a
  message naming the missing driver and `--channels chat`. A python adapter's project directory
  is put on `sys.path`, as `python -m` would from the project.
- **`hecklr run` exit codes.** 0 the run completed; 3 the run as a whole failed (it crashed, or
  every trial was a harness error); 4 the budget refused (estimate) or stopped (spend) the run;
  78 (`EX_CONFIG`) a configuration error; 2 a usage error (Click). 0-3 keep the gate's meanings;
  agent failures are results, not exit codes, until `hecklr gate` decides them.
- **Test injection.** Commands read optional `CliOverrides(llm_providers=...)` from Click's
  context object, so `CliRunner().invoke(app, args, obj=...)` scripts the models with no global
  state. `hecklr.testing.scripted_models()` is a cooperative scripted customer plus a rule-driven
  judge for `fake/` models, shipped so teams can smoke-test their wiring offline.
- **Run summary statistics.** `hecklr run` computes its summary with `hecklr_stats` directly:
  harness-error trials and trials with an `error` criterion grade are excluded and counted; the
  rest pass by `hecklr_grade.trial_passed`. The simulator contradiction rate is regenerated
  customer turns over customer turns, with a Wilson interval (turns of one trial are correlated,
  so the interval is optimistic; it is a health signal, not a gate). `hecklr-report`'s analysis
  (WS7b) supersedes this once merged.
- **Adaptive pass.** One extra pass of `ceil(0.5 x counted first-pass trials)`, allocated by
  `hecklr_stats.allocate_trials` against the baseline run (or the run itself, which favours the
  widest intervals), applied through `extend_matrix` and `run_additional`. One allocated trial
  of a scenario is one more trial index for each of its persona x channel cells.
- **pytest plugin.** Registered as a `pytest11` entry point and inert without `--hecklr-suite`.
  The package `hecklr` imports its API lazily so the plugin costs nothing in unrelated runs. One
  item per scenario; the suite runs once, on the first item. A scenario fails when every counted
  trial failed, or when the upper bound of its 95% Wilson interval is below
  `--hecklr-min-pass-rate` (default 0.8): failure needs evidence, not one unlucky trial.
- **Starter project.** `hecklr init` writes 5 chat scenarios, 3 personas and a world; without an
  agent URL it writes an in-process starter agent that calls the world's HTTP tools. Models
  default to a running Ollama, else an available API key; concurrency is 4 on Ollama (its
  default parallelism in `DEFAULT_PROVIDER_LIMITS`), 8 otherwise.

## Consequences

- Any grading behaviour change (phases, error grades) lands in `hecklr-grade` only.
- CI scripts can tell "the harness broke" (3) and "out of money" (4) from a finished run (0)
  without parsing output; `hecklr gate` adds 1 and 2 for verdicts.
- An HTTP agent cannot learn its trial's world URLs from the `http` adapter today (only
  `{{message}}`, `{{session_id}}`, `{{trial_id}}`), so remote agents in sandbox mode need a fixed
  sandbox address or observed mode; raised as an open question for WS6.
- Under `pytest -n`, each worker would run the whole suite; the plugin documents running in one
  process.
