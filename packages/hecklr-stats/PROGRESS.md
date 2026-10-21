# hecklr-stats progress

Owner: WS5. Spec: `docs/SPEC.md` → "Statistics and reporting", SC5, SC6, SC7, US-01, US-04, US-06,
US-11. Contracts: `hecklr_schemas.runtime` (`Estimate`, `MetricSummary`, `Comparison`,
`HumanLabel`, `Grade`), `hecklr_schemas.config.StatsConfig`.

## Plan (Wave 1)

1. `TrialOutcome` input record + per-scenario counts (no DuckDB dependency).
2. Wilson interval; suite pass rate with a two-stage cluster bootstrap; pass^k / pass@k (unbiased
   combinatorial estimators); latency quantiles with trial-block bootstrap CIs.
3. Paired comparison: Δ, paired cluster bootstrap CI, sign-flip permutation p (exact for small S),
   slices + Benjamini-Hochberg q, verdict rule, `trials_needed`.
4. Group-sequential Lan-DeMets O'Brien-Fleming boundaries; adaptive trial allocation.
5. Flaky scenario detection.
6. Judge calibration: Cohen's κ (bootstrap CI), precision/recall, trust flag, drift check.
7. Validation: reference-suite generator, SC5 A/A, SC6 power, CI coverage, property tests; size
   the reference suite and record it in `docs/adr/0005a-reference-suite-size-and-power.md`.

## Done

- Empty package scaffold (Wave 0): src layout, `py.typed`, smoke test.

## Next

- Steps 1-7 above.

## Open questions

- None yet.
