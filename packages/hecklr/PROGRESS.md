# hecklr (meta-package + CLI) progress

Owners: WS7a (composition, run/init/doctor/replay, SDK, pytest plugin); WS7c (compare, gate,
report, calibrate, model defaults, the GitHub Action in `action/`). Spec: `docs/SPEC.md` →
"Developer experience", "Run lifecycle", "Agent adapters", "Statistics and reporting".
Decisions: ADR 0007a, ADR 0007c.

## WS-wire (Wave 2b): voice and email in `hecklr run` (ADR 0007e, ADR 0005c, RFC 0010a)

Plan: (1) world URLs over every voice/email adapter; (2) issues #14 and #19; (3) compose voice
and email drivers, provider checks, recordings, RFC 0010a; (4) `calibrate --voice-loopback`
(#24); (5) flaky tests #20 and #28; (6) SC1 timing with luna (#13); (7) real CLI runs.

Done:
- World URLs on voice/email transports: `hecklr-adapters/tests/test_world_forwarding_voice_email.py`
  (pcm start message, Twilio customParameters, WebSocket handshake headers for all three
  protocols, Pipecat offer headers + `request_data`, LiveKit token metadata/attributes, email
  webhook headers + body, SMTP headers).
- #14: Wilson on the scenario count when every scenario is all-or-nothing
  (`test_us01_all_passing_suite_interval_does_not_collapse_at_small_n`, coverage test >= 90%).
- #19: `models_called` pre-flights only models the suite can call
  (`test_issue19_a_suite_that_calls_no_model_needs_no_key`, judge/simulator counterparts). The
  Action self-test's unstartable run now uses a missing agent entry point.
- Compose (`channels.py`, `config.py`): voice and email drivers; ASR/TTS/impairments checked in
  `prepare_run`; recordings in the run directory; webhook endpoint released after the run;
  `websocket` voice adapter selectable from `hecklr.yaml`. `tests/test_compose_channels.py`:
  real WebSocket voice agent (latency within 20 ms of the agent's 500 ms, recordings on disk and
  in the report), email webhook, SMTP/IMAP through GreenMail in Docker, `hecklr run --channels
  voice,email` through the CLI; each agent found its trial's world from what the adapter sent.
- #24: `hecklr calibrate --voice-loopback` (`test_sc4_calibrate_voice_loopback_measures_within_20ms`
  runs the installed console script). Found and fixed: DSP workers failed to start under the
  console script (duckdb import without `__main__`), `cli._keep_dsp_workers_off_the_entry_script`.
- #20 / #28: see the commit and the hecklr-voice / hecklr-adapters tests. Repeated full-suite
  `-n auto` runs: both tests passed in all 8 runs (the only other failure seen was the
  pre-existing hecklr-grade hypothesis bug, issue #38).
- #13 SC1 measured on this laptop with luna: `hecklr init --yes` (luna simulator and judge,
  concurrency 8) then `hecklr run` (15 trials): **44 s** wall, $0.0102. No change needed; the
  8.3 min in the issue was a shared local Ollama.
- Real CLI exercises (scratch projects): `hecklr run -s multi --channels voice,email` against
  `tests/fixtures/voice_agent.py` (WebSocket pcm) and `email_agent.py` (webhook): 4/4 ok,
  response latency p50 502-516 ms for a 500 ms agent; `hecklr compare` -> NO CHANGE (exit 0);
  `hecklr report latest -f html -f md -f json`; the HTML has a Voice section, 4 players, 6 WAVs.
  Email over smtp-imap through GreenMail in Docker: 2/2 ok. A guided luna caller over voice and
  email: 4/4 ok, $0.0011. `hecklr calibrate --voice-loopback`: all errors within 4 ms (idle).
  Found on the way: another process holding the webhook's default port 8787 makes every email
  trial a harness error (`Address already in use`); set `listen_port`.

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
- Package (WS7a): 120 tests, 98% line+branch coverage; `mypy --strict` and ruff clean.
- **WS7c** (ADR 0007c):
  - `hecklr run` prints `hecklr_report.render_terminal` (compared with `--baseline`) plus every
    scenario's Wilson interval and writes `report.html`; `summary.py`/`outcomes.py` deleted, the
    adaptive pass and pytest plugin use `hecklr_report.outcomes.convert`.
    `test_us01_run_prints_suite_and_scenario_pass_rates_with_intervals`.
  - `results.py` (run ids, `latest`, `baseline`), `reports.py` (md/html/json/junit files).
  - `hecklr compare`, `hecklr gate`, `hecklr report`: `tests/test_cli_compare_gate_report.py`, e.g.
    `test_us04_gate_exits_1_on_a_significant_regression`,
    `test_us04_gate_passes_an_unchanged_agent`, `test_gate_inconclusive_exit_code_is_configurable`,
    `test_gate_on_a_failed_candidate_run_is_a_harness_error`,
    `test_a_first_run_without_baseline_fails_or_passes_as_asked`,
    `test_us05_gate_comment_stays_under_30_lines`.
  - `hecklr calibrate --aa` / `--judges`: `tests/test_cli_calibrate.py`, e.g.
    `test_sc5_aa_runs_the_same_agent_twice_with_different_seeds`,
    `test_sc7_a_judge_humans_disagree_with_is_untrusted`.
  - Model defaults (`gpt-6-luna` > Anthropic > Ollama > fake with a warning; luna price written
    into `hecklr.yaml`; `fake/` models default to the scripted customer and judge); doctor's
    `models available` row. `test_models_follow_what_this_machine_has`,
    `test_doctor_reports_which_models_this_machine_can_use`.
  - PR #11 round-2 nits: providers (including judge-criterion `model` overrides) checked before
    the plan and run id print (`test_a_judge_criterion_model_without_its_key_is_a_config_error`);
    errors after the run is recorded are harness errors
    (`test_a_config_error_raised_mid_run_is_a_harness_error`).
  - `hecklr github` (hidden; the Action's plumbing over `gh`): `tests/test_cli_github.py`, e.g.
    `test_us05_one_sticky_comment_per_suite_is_created_then_updated`.
  - Package: 173 tests, 98% line+branch coverage; `mypy --strict`, ruff, actionlint and
    shellcheck (the action's `run:` blocks) clean.
  - **Real runs on the reference agent** (in process, rules brain, scripted `smoke` suite, 33
    scenarios, `action/selftest/`): clean 33/33; `hecklr run` + `hecklr gate` against the clean
    baseline: F1 exit 1 (−27.3 pts [−42.4, −12.1], p=0.004), F2-F6 exit 1 (−18.2 pts
    [−33.3, −6.1], p 0.027-0.035), clean exit 0 (NO CHANGE). `calibrate --aa --runs` on two
    clean runs: passed, simulated false-alarm rate 0.0% [0.0%, 1.9%] (33 x 1, 200 comparisons).
    `hecklr report -f html -f junit -f json -f md` wrote all four files.
  - **Action proven on a real draft PR** (#18, closed, branch deleted): self-test job green
    (baseline 0, clean 0, F3 1); sticky comments created, then updated on re-run (2 comments,
    not 4); a push to the branch stored `hecklr-baseline-*` and the PR run fetched it and gated F5
    as REGRESSION (check red, comment updated).
  - **Review round (PR #22)**: the Action gates the run its own `hecklr run` recorded
    (`hecklr github latest-run --since`), exits 3 when the run failed or recorded nothing, and
    clears stale reports first; `gate`/`compare` refuse a candidate equal to the baseline and
    `calibrate --aa` the same run twice. Tests:
    `test_us04_a_run_that_recorded_nothing_is_never_gated_against_the_fetched_baseline`,
    `test_us04_gate_of_a_run_against_itself_is_a_harness_error`,
    `test_gate_that_breaks_while_writing_reports_is_a_harness_error`,
    `test_latest_run_since_keeps_only_runs_this_job_made`,
    `test_sc5_aa_refuses_the_same_run_twice`. Self-test workflow adds a run that cannot start
    (a model without price or key) and requires exit 3. Reproduced locally on the reference
    agent: run exit 78, `latest-run --since` exit 3, gate of `latest` exit 3 (was exit 0 PASS);
    F1 candidate still exit 1, clean exit 0. Package: 182 tests, 98% line+branch coverage.
- Exercised for real on local Ollama (`qwen3:4b-instruct`, shared with other workstreams): init →
  doctor → run of the starter project (15 trials, 15 ok, suite 93% [73%, 100%]) took 8.3 min wall,
  over SC1's 5 min because the shared Ollama kept swapping models; against the fixture FastAPI
  agent via a probed `http` adapter (10 trials, suite 20% [0%, 60%], only the FAQ scenario passes,
  as expected of an agent without tools).

## Next

- Pass judge calibrations (from the UI's labels, WS16) into reports and `gate`.
- When the schema carries RFC 0010a's `websocket` voice member: delete `hecklr/config.py` and
  load with `hecklr_core.load_project`.

## Open questions

- SDK users whose own `__main__` imports duckdb (e.g. `hecklr_core` at module top) hit the same
  DSP-worker start failure the console script had; the general fix belongs in `hecklr-voice`'s
  pool (issue #31).

## Decisions

- ADR 0007e: voice/email drivers, provider defaults and checks, recordings, world URLs on
  every transport, RFC 0010a shim, `--voice-loopback`. ADR 0005c: suite interval at the
  all-or-nothing boundary.
- ADR 0007c: run aliases, gate only compares (3 on any lookup failure, `--missing-baseline`),
  A/A with seeds s and s+1, judges from JSONL labels, scripted `fake/` models, model defaults,
  the Action's `hecklr github` plumbing.
- ADR 0007a: grading ownership, composition steps, exit codes, CLI test injection, run summary
  statistics, adaptive pass size, pytest pass rule, starter project.
