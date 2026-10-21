# 0001a. Orchestrator: loading conventions, trial execution and per-run DuckDB storage

Date: 2026-10-21 · Status: accepted · Workstream: WS1

## Context

`hecklr-core` turns `hecklr.yaml` into a stored, graded run (spec → "Run lifecycle", "Orchestrator").
The frozen schemas fix the data and the trial-loop Protocols (ADR 0006) but leave open where
personas live, how graders learn which criterion they answer, what "hard budget" means for
in-flight trials, and how results are laid out on disk. WS7 wires the concrete simulator, world,
drivers and graders next wave, so the orchestrator must depend on Protocols only.

## Decision

- **Loading.** YAML is composed with PyYAML node marks, so a Pydantic error path maps to a file and
  line; the timestamp resolver is dropped (ISO times stay strings) and duplicate keys are errors.
  Every invalid file of a suite is reported at once. Personas referenced by scenarios are looked up
  in `personas/**/*.yaml` next to `hecklr.yaml`; a suite's `personas` glob replaces scenario
  personas for every scenario. A cell's channels are the scenario's, intersected with the agent's
  configured adapters and the suite (or `--channels`) filter; explicitly asking for a channel with no
  adapter is an error. Voice cells need a persona voice profile; adversarial scenarios need
  adversarial personas; scenario `world` must be the project's world.
- **Config hash and seeds.** SHA-256 of canonical JSON of config, world, the suite's scenarios and
  personas, plus suite name, trials and channels. Trial seed = top 63 bits of SHA-256 of
  `[run_seed, scenario, persona, channel, index]`, so baseline and candidate cells pair.
- **Cost estimate.** An upper bound: conversations run to `limits.max_turns`; the simulator speaks
  every other turn and re-reads the dialogue; each judge vote reads the whole transcript (constants
  in `planning.py`). Unknown paid models fail closed (ADR 0003).
- **Executor.** `RunExecutor` takes `Collaborators` (sandbox, a driver per channel, simulator
  factory, simulator and judge `LLMProvider`s, grader factory). The grader factory returns
  `CriterionGrader(grader, criterion_index)` pairs, so the executor can fill
  `GradeContext.criterion`; WS7 adapts `hecklr_grade.build_graders` to it. Trials run in one anyio
  task group under a semaphore of `budget.concurrency`. Harness errors (any exception from world,
  simulator or driver) are retried up to 3 attempts with seeded exponential backoff, except
  provider-layer configuration errors and non-retryable provider errors; only the last attempt's
  events are stored. A trial the driver fails to end by `max_duration_s + 30 s` is cancelled and
  recorded `timeout`. Grader exceptions become `error` grades.
- **Budget.** Spend is the sum of `LLMResponse.cost_usd` (cache hits are free), charged per trial
  through a metering wrapper. Crossing `budget.usd` stops new launches and cancels in-flight trials;
  the run ends `budget_exceeded` with finished trials kept. A trial whose grading crossed the budget
  is dropped, since its grades are incomplete. Money already spent is always counted in
  `Run.spent_usd`.
- **Model stack.** `CachedProvider(RateLimitedProvider(ProviderRouter))`: the cache
  (`.hecklr/cache/llm.sqlite3`) sits outside the per-prefix concurrency and request-rate limits, so
  hits never wait for a slot.
- **Storage.** One directory per run, `.hecklr/runs/<run_id>/`, with `results.duckdb` (tables
  `runs`, `trials`, `events`, `grades`, view `runs_latest`). Rows are appended only; each keeps the
  full model JSON in `record` (exact round trip) beside queryable columns. Rows are inserted as one
  JSON array per batch through `from_json_strict`: DuckDB's Python client binds parameters at about
  2 ms per row, which alone would break the 1,000-trial target. All database work runs on one worker
  thread per store. `run.json` (newest `Run`) and Parquet files are exports, rewritten atomically,
  so other processes list and read runs without opening a database being written.
- **Observability.** structlog contextvars bind `run_id` and `trial_id`; OpenTelemetry spans
  `hecklr.run` and `hecklr.trial` use the global tracer (no-op unless the app configures one).

## Consequences

- WS7 composes everything from `load_project` → `resolve_suite` → `plan_run` → `open_llm` →
  `RunExecutor`; WS5's adaptive policy calls `extend_matrix` and `run_additional`.
- Projects keep personas under `personas/`; another location needs a config field (an RFC).
- Opening a run database being written by another process fails; readers use `run.json` and the
  Parquet exports, or wait for the run to end.
- Cancelling in-flight trials on budget exhaustion wastes their partial spend, in exchange for a
  cap that is never exceeded by more than the calls already in flight.
