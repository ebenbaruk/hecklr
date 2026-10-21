# hecklr-core progress

Owner: WS1. Spec: `docs/SPEC.md` → "Run lifecycle", "Core data model", "Orchestrator (hecklr-core)",
"Adaptive trials", "Configuration", "Engineering standards". Decisions: ADR 0001a.

## Done

- **Loading** (`loading.py`, `yaml_files.py`): `load_project`, `resolve_suite` (scenario and persona
  discovery, tag and channel filters, `--trials`/`--channels` overrides), cross-reference checks
  (persona ids, world id, adversarial personas, voice profiles, adapters), duplicate ids, config
  hash. Errors name file and line; ISO timestamps stay strings; duplicate YAML keys are refused.
  Tests: `tests/test_loading.py` (28), e.g. `test_validation_error_names_file_and_line_of_the_bad_field`.
- **Planning** (`planning.py`): matrix expansion, SHA-256 seeds (pinned value, Hypothesis range
  check), cost estimate from `limits.max_turns`, refusal over budget, `extend_matrix` for adaptive
  trials. Tests: `tests/test_planning.py` (13), e.g. `test_plan_run_refuses_when_estimate_exceeds_budget`.
- **Model stack** (`llm.py`, `metering.py`, `sink.py`): `open_llm` =
  `CachedProvider(RateLimitedProvider(ProviderRouter))` at `.hecklr/cache/llm.sqlite3`; per-prefix
  concurrency and token-bucket limits; per-trial cost metering against a hard run budget;
  `TrialEventSink`. Tests: `tests/test_llm_stack.py` (9), e.g.
  `test_open_llm_caches_responses_under_the_output_dir`.
- **Storage** (`storage.py`): `ResultStore` with `write_run`, `append_trial`, `append_events`,
  `append_grades`, `append_trial_result`, `export`, `load_run`, `list_runs`, `latest_run`.
  Tests: `tests/test_storage.py` (12), e.g. `test_every_model_round_trips_exactly`,
  `test_arbitrary_text_and_json_round_trip` (Hypothesis), `test_writes_do_not_block_the_event_loop`.
- **Executor** (`executor.py`): `RunExecutor.execute` / `run_additional`, retries, deadline,
  budget stop, grading, structlog context, OpenTelemetry spans. Tests: `tests/test_executor.py`
  (27), e.g. `test_budget_exceeded_mid_run_stops_cleanly_and_keeps_finished_trials`,
  `test_harness_error_after_three_attempts_is_recorded_and_excluded`,
  `test_adaptive_trials_append_to_the_same_run`.
- **WS1 Done criterion**: `test_ws1_thousand_fake_trials_under_sixty_seconds` runs 1,000 fake trials
  (2 graders each) in about 5 s on the dev machine (M-series, 10 cores); runs in normal CI.
- Package: 90 tests, 100% line and branch coverage; `mypy --strict` and ruff clean.
- Exercised for real: a scratch project over local Ollama (`qwen3:4b-instruct`) through the real
  router, rate limiter and cache: 6 trials in 11.1 s (12 cache misses, $0); the identical rerun took
  0.1 s with 12 hits. DuckDB tables, `run.json` and Parquet files inspected.

## Next

- WS7 wires concrete components: `Collaborators(sandbox=hecklr_world..., drivers={chat: ...},
  simulator_factory=hecklr_sim.make_simulator, simulator_llm=llm, judge_llm=llm,
  grade_trial=<callable over hecklr_grade.build_graders + grade_trial>)` (done by WS7a, ADR 0007a).
- WS5 adaptive policy calls `extend_matrix(run.matrix, run.seed, scenario_ids, n)` and
  `RunExecutor.run_additional(run, specs)`.

## Open questions

- Persona location is a convention (`personas/**/*.yaml`); a `personas` field in `hecklr.yaml`
  would need an RFC if projects ask for another layout.
- Provider rate limits are code defaults (`DEFAULT_PROVIDER_LIMITS`); `hecklr.yaml` has no field
  for them yet.

## Decisions

- ADR 0007a (WS7a): grading is delegated to an injected `TrialGrader` callable
  (`Collaborators.grade_trial`); `CriterionGrader` and `GraderFactory` are gone.

- See ADR 0001a: loading conventions, seed derivation, estimate heuristics,
  retry and budget semantics, per-run DuckDB with JSON-batch inserts (row-by-row parameter binding
  measured at about 2 ms per row in DuckDB 1.5's Python client).
