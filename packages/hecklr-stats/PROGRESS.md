# hecklr-stats progress

Owner: WS5. Spec: `docs/SPEC.md` → "Statistics and reporting", SC5, SC6, SC7, US-01, US-04, US-06,
US-11. Contracts: `hecklr_schemas.runtime` (`Estimate`, `MetricSummary`, `Comparison`,
`HumanLabel`, `Grade`), `hecklr_schemas.config.StatsConfig`. Decisions:
`docs/adr/0005a-reference-suite-size-and-power.md`, `docs/adr/0005b-comparison-decisions.md`.

## Done (Wave 1)

- `TrialOutcome` (scenario, passed, slice labels, tags, latencies) and `ScenarioCounts`: the only
  inputs, so no DuckDB dependency. WS1/WS7 build outcomes from `ok` trials and grades.
- Intervals: Wilson (`test_wilson_matches_reference_values`, exact coverage
  `test_wilson_coverage_averages_to_nominal`), two-stage cluster bootstrap for suite pass rate,
  pass^k and pass@k (unbiased combinatorial estimators, checked against subset enumeration),
  latency quantiles with trial-block bootstrap (`test_us06_latency_quantiles_have_bootstrap_intervals`).
- Paired comparison: Δ, paired cluster bootstrap CI, sign-flip p (exact ≤ 16 scenarios, seeded
  Monte Carlo above), BH q-values across slices, the spec verdict rule, `trials_needed`
  (`test_compare.py`: 28 tests incl. hypothesis properties for BH monotonicity and verdict
  consistency/mirror symmetry).
- Adaptive: Lan-DeMets O'Brien-Fleming boundaries by recursive integration, matching rpact for
  2, 3 and 5 looks (`test_boundaries_match_published_values`); overall false-alarm rate 0.050 in
  a 400k-path simulation (`test_repeated_looks_keep_the_overall_false_alarm_rate`); `look` and
  `alpha_spent` recorded on `Comparison`; `allocate_trials` (variance-reduction greedy, flips 2x).
- Flaky detection (`flaky_scenarios`, [0.2, 0.8] inclusive, pooled across identical runs).
- Judge calibration: Cohen's κ with bootstrap CI, precision/recall (fail = positive) with Wilson
  CIs, trust at κ ≥ 0.70, `judge_drift` with a paired bootstrap of the κ change.
- `summarize_run` → `MetricSummary` list; `compare_runs` → `Comparison` list.
- Validation (`test_validation.py`, all seeded):
  - SC5: A/A regression rate at 60 x 5, 200 comparisons: ≤ 0.05 (0.020 with the default 10k
    resamples in a manual run; 0.018 over 2,000 replicates in ADR 0005a).
  - SC6: power for a 10-point drop at 60 x 5: ≥ 0.80 (0.885 manual run, 0.906 in ADR 0005a);
    concentrated on 30% of scenarios: 0.804.
  - Interval calibration: suite pass rate 0.955-0.968, pass^3 0.93-0.97, Δ ≥ 0.92 coverage at
    nominal 0.95.
- **Reference suite size: at least 60 scenarios x 5 trials per run** (ADR 0005a). WS8 sizes the
  reference agent's suite from this.
- Performance: `compare_runs` on 200 scenarios x 10 trials with 10,000 resamples ≈ 0.1 s per
  slice (`test_full_comparison_on_200_by_10_with_10k_resamples_is_fast`).
- 120 tests, 100% line and branch coverage, `mypy --strict` and ruff clean.

## Next

- WS7: call `summarize_run` and `compare_runs` (pass `config.stats` and the run seed), read
  `comparisons[0]` for the gate, show slice verdicts as diagnostics.
- Voice wave: a latency comparison between runs (p95 delta with CI and a verdict) for the F7
  latency gate; it is not needed by the chat milestone.
- Measure the adaptive design's trial savings (spec: "to be measured and published").

## Open questions

- `trials_needed` counts extra trials per scenario in each run; adaptive allocation adds trials to
  the candidate only (the baseline is a stored artifact). WS7 should word the report accordingly.
