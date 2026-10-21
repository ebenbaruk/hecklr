# hecklr (meta-package + CLI) progress

Owner: WS7a (composition, run/init/doctor/replay, SDK, pytest plugin); WS7b owns `hecklr-report`
and the compare/gate/report commands. Spec: `docs/SPEC.md` → "Developer experience", "Run
lifecycle", "Agent adapters". Decisions: ADR 0007a.

## Plan (WS7c, Wave 2a)

1. `hecklr run` summarizes with `hecklr_report.analyze` + `render_terminal` (plus every scenario's
   Wilson interval, US-01) and writes `report.html`; `summary.py` / `outcomes.py` deleted.
2. `hecklr compare`, `hecklr gate`, `hecklr report` over stored runs (`results.py`: ids and the
   `latest` / `baseline` aliases).
3. `hecklr calibrate --aa` (two runs of the same agent + simulated A/A false-alarm rate at the
   suite's size) and `--judges` (human labels file -> Cohen's kappa per rubric).
4. `hecklr init` model defaults (OpenAI `gpt-6-luna`, Anthropic, Ollama, fake); `doctor` says which.
   PR #11 round-2 nits: providers checked before the run id is shown, judge-criterion model
   overrides checked, config-error mapping only before the run is recorded.
5. `action/`: composite GitHub Action; proven on a throwaway draft PR.

## Done

- **Composition** (`compose.py`): `prepare_run` / `execute_run` bind world sandbox, chat driver +
  adapter, simulator, `hecklr_grade` grading and the cached model stack; adaptive second pass;
  voice/email suites refused with the missing driver named. Tests: `tests/test_compose.py`, e.g.
  `test_us01_run_stores_graded_trials_for_every_scenario`,
  `test_adaptive_pass_weighs_scenarios_that_flipped_vs_the_baseline`,
  `test_voice_suite_names_the_missing_driver`.
- **Integrator findings**: 1 and 2 (core delegates grading to an injected `TrialGrader`, the
  composition passes `build_graders` + `grade_trial`; `hecklr-core` tests updated, 100% coverage),
  4 (`hecklr_sim.derive_seed` renamed `derive_subseed`). Finding 3 is WS7b's; `outcomes.py` is the
  minimal version `hecklr run` needs.
- **CLI** (`cli.py`, `commands/`): `run`, `init`, `doctor`, `replay`; other spec commands are stubs
  in `commands/stubs.py` (exit 69). `-v/-vv` for harness logs. Tests: `test_cli_run.py` (e.g.
  `test_us01_run_prints_suite_and_scenario_pass_rates_with_intervals`,
  `test_init_and_run_against_a_real_http_agent`), `test_cli_init.py` (e.g.
  `test_sc1_init_yes_writes_a_project_with_five_scenarios_and_three_personas`), `test_doctor.py`,
  `test_cli_replay.py` (`test_us02_replay_writes_scenarios_and_reports_skipped_conversations`).
- **SDK**: `@hecklr.agent`, `hecklr.Session`, `hecklr.run` / `hecklr.run_async`; `hecklr.testing`
  scripted models. Tests: `test_sdk.py`, `test_api.py`.
- **pytest plugin** (`pytest_plugin.py`, `pytest11` entry point): `test_pytest_plugin.py`, incl. a
  real `python -m pytest -p hecklr --hecklr-suite ci` subprocess run.
- **E2E**: `tests/e2e/test_sc1_quickstart.py::test_sc1_init_then_run_stores_a_graded_run` (init
  `--yes` then run through the CLI, 15 graded trials, about 3 s).
- Review round 1: the pytest plugin runs a suite at most once even when it raises
  (`test_a_crashing_suite_runs_once_and_fails_every_scenario`) and fails every item of a run
  that did not complete (`test_a_run_that_did_not_complete_fails_every_scenario`); a missing
  provider key exits 78 before the run is recorded
  (`test_a_missing_provider_key_is_a_config_error_before_the_run_starts`); a crashed run exits 3
  naming every cause (`test_a_crashed_run_is_a_harness_error_naming_every_cause`); `init`
  refuses `--adapter python --agent-url` and explains a failed validation.
- Package: 120 tests, 98% line+branch coverage; `mypy --strict` and ruff clean.
- Exercised for real on local Ollama (`qwen3:4b-instruct`, shared with other workstreams): init →
  doctor → run of the starter project (15 trials, 15 ok, suite 93% [73%, 100%]) took 8.3 min wall,
  over SC1's 5 min because the shared Ollama kept swapping models; against the fixture FastAPI
  agent via a probed `http` adapter (10 trials, suite 20% [0%, 60%], only the FAQ scenario passes,
  as expected of an agent without tools).

## Next

- Wire `compare`, `gate`, `report` once `hecklr-report` (WS7b) merges; replace `summary.py` /
  `outcomes.py` with its `analyze` + `render_terminal`.
- Voice and email drivers plug into `compose.build_drivers` when WS9/WS10/WS12 land.

## Open questions

- HTTP agents in sandbox mode cannot learn their trial's world URLs from the `http` adapter
  (no `{{world_http_url}}` placeholder or header). Needs a WS6 decision (adapter change, no
  schema change: `SessionContext` already carries the URLs).
- SC1 on a laptop with a local model is model-bound (8.3 min measured): issue #13.
- The suite pass rate's cluster-bootstrap interval collapses to [100%, 100%] when every trial
  passes: issue #14 (WS5/WS7b).

## Decisions

- ADR 0007a: grading ownership, composition steps, exit codes, CLI test injection, run summary
  statistics, adaptive pass size, pytest pass rule, starter project.
