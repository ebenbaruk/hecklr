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
- 73 tests at first review, 98% line+branch coverage, `mypy --strict` and ruff clean.
- Review round 1: damaged recordings no longer break `render_html`
  (`test_truncated_recording_is_drawn_up_to_its_last_whole_frame`,
  `test_unreadable_recording_keeps_markers_and_says_why[empty|not-riff|zero-rate]`,
  `test_header_only_recording_plays_without_a_waveform`); recording paths confined to the run
  directory (`test_recording_paths_outside_the_run_directory_are_not_embedded`); slice deltas and
  headline/latency arrow values carry intervals (`test_us05_every_slice_delta_carries_its_interval`,
  `test_us05_headline_values_carry_their_intervals`); `@` mentions neutralized
  (`test_escape_breaks_github_mentions`); XML-forbidden characters dropped from JUnit
  (`test_junit_drops_characters_xml_cannot_carry`). 83 tests, 98% line+branch coverage.
- Exercised for real: rendered a 108-trial chat+voice regression report, served it on
  127.0.0.1 and drove it in Chrome: one network request (the document), no page errors,
  waveform and markers drawn, failed-only and text filters work, evidence links open the
  timeline row.
- **WShstats (Wave 6 close, ADR 0024-hstats):**
  - #101: conversations the consistency guard gave up on count in the contradiction rate and are
    named in every format (`test_analysis.py`, `test_markdown.py`,
    `test_machine_and_terminal.py`).
  - #103: adaptive runs are compared at O'Brien-Fleming looks, first pass then everything
    (`test_adaptive.py`: `test_issue103_*`, 6 tests).
  - #102: scenarios flaky across runs declared identical (`identical_runs=True`, only from
    `calibrate --aa`; a shared commit is not enough because it misses uncommitted edits) carry
    `ScenarioResult.flaky` with its Wilson interval, shown in the terminal, PR comment and HTML,
    capped at 4 plus "+N more" (`test_flaky.py`, 5 tests, incl.
    `test_issue102_runs_sharing_a_commit_are_not_assumed_identical`); `Analysis.judge_model_change`
    warns in every format when baseline and candidate were graded by different judge models
    (`test_judge_model_change.py`, 3 tests). The UI reads the report JSON: `scenarios[].flaky`
    and both runs' `judge_model` are in it; rendering them there is left to the UI owner.
  - #104: the PR comment's untrusted judges show κ with its interval
    (`test_issue104_untrusted_judges_kappa_carries_its_interval`).
  - 112 tests, 98% line+branch coverage.

## Next

- WS7a wiring (next batch): `hecklr compare`, `hecklr gate`, `hecklr report` call `analyze` with
  `personas=project personas`, and write `report.html` into the run directory.
- Judge calibrations come from the UI labeling queue (WS16); pass them as `judge_calibrations`.
- Chat TTLT (ADR 0006: `latency_ms + end_ms - t_ms`) is not yet computed or reported; add it as
  its own latency metric next to TTFT.
- Run-level `response_latency` summaries pool chat TTFT and voice response latency in
  mixed-channel runs (only the comparisons split by channel); summarize per channel too.

## Open questions

- Latency verdicts are diagnostics only; gating on them needs a `GateConfig` field (RFC).
