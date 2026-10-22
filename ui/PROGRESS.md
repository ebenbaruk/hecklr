# Local UI (`hecklr ui`) progress

Owner: WS16. Spec: `docs/SPEC.md` → "Local UI", US-03, US-08, US-11, "Judge calibration".
Decisions: ADR 0016a. Backend: `packages/hecklr/src/hecklr/ui_api/`, command
`packages/hecklr/src/hecklr/commands/ui.py`. Frontend: this directory.

## Done

- Backend read side (`runs.py`, `trials.py`): runs from `run.json`, results from Parquet exports
  or read-only DuckDB, a run being written answers 409; analyses via `hecklr.results`
  (`test_runs.py`: `test_us01_run_summary_reports_pass_rate_and_cost_with_intervals`,
  `test_us05_comparison_has_verdict_deltas_with_ci_and_flips_by_cause`,
  `test_us03_trial_viewer_has_transcript_tools_world_diff_and_judge_rationale`,
  `test_us03_voice_trial_has_recordings_with_turn_markers`,
  `test_a_run_being_written_is_listed_but_its_results_are_busy` (another process holds the DB),
  `test_recordings_outside_the_run_or_not_wav_are_never_served`).
- Labels (`labels.py`): `.hecklr/labels.jsonl`, stratified seeded queue, κ agreement
  (`test_labels.py`: `test_us11_*`, `test_sc7_*`,
  `test_the_labels_file_is_what_hecklr_calibrate_judges_reads`).
- Review (`review.py`): clusters, proposals; approval validated by `hecklr_core.load_model` plus
  world, persona-library and id-uniqueness checks over `scenarios/**` and every suite's
  `scenarios` glob, warning when no suite runs the written file (`test_review.py`:
  `test_us08_approving_writes_a_scenario_the_suite_loader_accepts` checks the result with
  `resolve_suite`, `test_us08_an_id_used_under_any_suite_glob_is_refused`,
  `test_us08_approval_warns_when_no_suite_runs_the_written_scenario`).
- Local-only guard: `Host` must be local (or `--host`) else 400
  (`test_other_host_names_are_refused_against_dns_rebinding`, `test_allowed_hosts_follow_the_bind`).
- `hecklr ui` (`test_command.py`), wheel packaging (`artifacts`), `--dev`.
- React app: 7 views, dark mode, responsive, accessible labels; Vitest 47 tests, 93% lines.
- Python: 58 test functions (75 cases with parametrization) in `tests/ui_api`, 99% coverage of
  `ui_api` + `commands/ui.py`.
- Real store: acme-maintenance smoke (clean 100.0% [89.6%, 100.0%] vs F1 72.7% [55.8%, 84.9%],
  regression Δ −27.3 pts [−42.4, −12.1]), a `ci` run on fake models (judge grades), acme-voice
  `voice-timing` (recordings). Playwright `e2e/ui.spec.ts`: 7/7 against `hecklr ui` installed
  from the built wheels in a fresh venv.

## Next

- When WS13 (ADR 0013a, compatible layout) merges, delegate `review.py` to `hecklr_mine.load_queue` / `decide`.

## Open questions

- None: WS13's ADR 0013a layout (mine/<id>/*.jsonl, O_APPEND whole-line writes) matches ADR 0016a.
