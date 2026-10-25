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
  (2 graders each) in about 5 s on the dev machine (M-series, 10 cores); `perf`-marked, so it runs
  in CI's serial benchmark step since ADR 00hcb.
- Package: 90 tests, 100% line and branch coverage; `mypy --strict` and ruff clean.
- Exercised for real: a scratch project over local Ollama (`qwen3:4b-instruct`) through the real
  router, rate limiter and cache: 6 trials in 11.1 s (12 cache misses, $0); the identical rerun took
  0.1 s with 12 hits. DuckDB tables, `run.json` and Parquet files inspected.

## Done (WShcore, Wave 5 hardening)

- **#76 cost estimate is an upper bound on judge spend** (ADR 00hca). Judges are charged both
  world snapshots (sized from the loaded world, `world_snapshot_chars`), tool-heavy transcript
  lines, JSON-dense tokens (2 chars/token), `max_tokens` answers and one retry per vote;
  `injection_resisted` and `custom` criteria are charged too. Tests: `tests/test_planning.py`
  (`test_issue76_*`, 5 new) and `examples/acme-maintenance/tests/test_cost_estimate.py`: the
  issue's 51-trial `matrix` run, judge billed at luna's rate and density ($0.0183, as the issue's
  real run), finishes with `--budget-usd` set to its own estimate ($0.096), also when every vote
  is retried. Before: estimate $0.0071 for that run.
- **SC2** (ADR 00hcb): `tests/perf/test_sc2_200_scenarios_x3_trials_under_10_min_at_concurrency_32`,
  marker `perf`, skipped under xdist, serial CI step `uv run pytest -m perf -p no:xdist`.
  Measured 2026-10-23 on the dev machine (M-series, 10 cores, shared with other builders): 600
  trials in **24.1 s** (load average 11-21), 8.9 turns per trial, throughput 35.8 trials/s
  [33.9, 38.0], trial duration median 200 ms [193, 204], p95 561 ms [356, 751] (95% CIs). A first
  run at load average 90-146 took 20.6 s with a lighter customer. With 25x headroom under the
  10-minute bound, no bottleneck needed fixing.
- Package: 102 tests (one `perf`), 100% line and branch coverage with the serial step.

## Done (WShrun, Wave 6-close)

- **#117** a cancelled run (Ctrl-C, SIGTERM) exports its finished trials and `ResultStore` closes
  its databases under a shielded scope: `test_issue117_a_cancelled_run_keeps_its_counts_exports_
  and_closes_its_store`.
- **#85** the estimate charges every simulator draft (3 per customer turn) and its feedback
  (ADR 0024-hrun): `test_issue85_estimate_uses_max_turns_prices_and_every_simulator_draft`.

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
