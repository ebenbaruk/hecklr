# hecklr-report progress

Owner: WS7b. Spec: `docs/SPEC.md` → "Statistics and reporting" (Report formats, Metrics), US-01,
US-03, US-04, US-05, US-06, US-07, "Voice simulation engine > Metrics". Decisions:
`docs/adr/0007b-report-analysis-and-gate.md`.

## Plan (Wave 1b)

1. `analysis.py`: `analyze(candidate, baseline, stats, scenarios)` → frozen `Analysis`
   (conversion of stored results to `TrialOutcome`, summaries, comparisons, flips, counts, voice,
   judge trust).
2. Renderers: terminal (Rich), Markdown PR comment (< 30 lines), self-contained HTML, JSON, JUnit.
3. `gate_exit_code(analysis, gate)`.

## Done

- `outcomes.py`: stored results → `TrialRow` + `hecklr_stats.TrialOutcome`; trial verdict rule
  (harness errors excluded, agent errors/timeouts fail, failing grade beats error grade,
  error-only = undecided); slice labels channel/persona/snr bucket/accent; failure causes from
  the failing criterion. Tests: `test_harness_errors_are_excluded_but_counted`,
  `test_grade_errors_leave_a_trial_undecided_unless_another_criterion_failed`,
  `test_agent_errors_fail_even_when_every_grade_passed`, `test_us07_snr_buckets`,
  `test_criterion_labels_name_what_failed`.
- `analysis.py`: suite pass rate (cluster bootstrap), per-scenario Wilson, pass^3, pass@3,
  latency p50/p95/p99, cost per trial, all with intervals; pass-rate comparisons with BH q-values
  via `hecklr_stats.compare_runs`; latency comparisons (`latency.py`); flips grouped by cause;
  contradiction rate (Wilson); judge trust. Tests: `test_us04_bad_candidate_is_a_regression`,
  `test_us04_identical_agent_is_not_a_regression`,
  `test_us01_pass_rate_with_interval_per_scenario_and_suite`,
  `test_latency_regression_is_detected_and_reported_per_channel` (+700 ms detected),
  `test_identical_latencies_are_not_a_latency_regression`, `test_problems_block_comparison`.
- `voice.py`: US-06 turn-taking metrics per SNR bucket and accent (response latency and barge-in
  quantiles, false interruptions per 100 turns, dead air per call, backchannel stops, talk-over),
  reusing `hecklr_grade.measure`; US-07 degradation curve. Test:
  `test_us06_us07_voice_slices_by_noise_and_accent`.
- Renderers: `render_terminal`, `render_markdown` (property test over up to 60 causes x 40
  scenarios, 30 slices, 10 judges: always < 30 lines — `test_us05_comment_stays_under_30_lines`),
  `render_html` (self-contained, XSS-escaped, transcript, tool calls, world diff, grades with
  evidence links, audio player + canvas waveform + turn markers, audio budget),
  `render_json` (round-trips), `render_junit` (ElementTree-parsed structure).
- `gate_exit_code`: `test_us04_exit_code_follows_the_suite_verdict` (all verdicts x both
  `on_inconclusive` settings), problems and missing baseline exit 3.
- 74 tests, 98% line+branch coverage, `mypy --strict` and ruff clean.
- Exercised for real: rendered a 108-trial chat+voice regression report, served it on
  127.0.0.1 and drove it in Chrome: one network request (the document), no page errors,
  waveform and markers drawn, failed-only and text filters work, evidence links open the
  timeline row.

## Next

- WS7a wiring (next batch): `hecklr compare`, `hecklr gate`, `hecklr report` call `analyze` with
  `personas=project personas`, and write `report.html` into the run directory.
- Judge calibrations come from the UI labeling queue (WS16); pass them as `judge_calibrations`.

## Open questions

- Latency verdicts are diagnostics only; gating on them needs a `GateConfig` field (RFC).
