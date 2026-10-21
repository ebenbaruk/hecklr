# 0005b. How hecklr-stats turns two runs into verdicts

Date: 2026-10-21 · Status: accepted · Workstream: WS5

## Context

The spec fixes the statistics (paired by scenario, cluster bootstrap, sign-flip permutation,
Benjamini-Hochberg across slices, the verdict table, O'Brien-Fleming alpha spending) but leaves
several choices open that change what a user sees. These are recorded here so reports, the gate
and the fixer read them the same way.

## Decision

- **Primary hypothesis vs slices.** The whole-suite comparison (slice `{}`) is decided on its own
  p-value; its `q_value` equals its `p_value`. Slice comparisons (each label value and each tag)
  form one Benjamini-Hochberg family per run and are decided on their q-values. Putting the suite
  in the family would make the gate depend on how many slices a team defines.
- **Interval level.** Comparison intervals use confidence `1 - alpha`, so "p < α" and "the
  interval excludes 0" tell the same story.
- **Pairing.** Only scenarios present in both runs are compared; `n_scenarios` says how many. A
  slice with no shared scenario is not reported.
- **Bootstrap.** Two stages: resample scenarios, then each run's trials within each drawn scenario.
  For pass/fail trials the second stage is a binomial draw at the observed rate, which is exact and
  vectorizes. A 200 x 10 comparison with 10,000 resamples takes about 0.1 s per slice. The second
  stage counts within-scenario noise twice, so pass-rate intervals are slightly conservative
  (measured 0.955-0.968 coverage at nominal 0.95). For nonlinear metrics (pass^k, pass@k) the
  replicates are recentred on the point estimate to remove the second stage's bias (pass^3
  coverage 0.89 before, 0.93-0.97 after).
- **Permutation test.** Two-sided sign flips of per-scenario differences, exact for up to 16
  scenarios (65,536 patterns), otherwise seeded Monte Carlo with `(1 + hits) / (1 + B)`, which
  keeps the test valid. With 5 or fewer scenarios the test can never reach α = 0.05.
- **trials_needed.** Assumes the observed Δ is true and the standard error shrinks with
  `1/sqrt(trials per scenario)`. For `|Δ| > δ` it asks for significance and for the estimate to
  land beyond δ; for `|Δ| <= δ` it asks for an interval inside `[-δ, +δ]`; each with 80% chance.
  The count is extra trials per scenario in each run. It is `None` when trials cannot decide:
  too few scenarios (add scenarios), a Δ sitting on δ, or more than 1,000 trials. Between-scenario
  spread does not shrink with trials, so the count is a lower bound when scenarios disagree.
- **Alpha spending.** Lan-DeMets O'Brien-Fleming with α/2 spent per side, the convention of
  rpact, gsDesign and ldbounds, so boundaries match published tables (5 looks: 4.877, 3.357,
  2.680, 2.290, 2.031). Boundaries come from recursive numerical integration on a fixed grid
  (deterministic), not from a randomized multivariate-normal integrator. Looks must be at least 1%
  of information apart. At each look the permutation p-value is compared with the look's nominal
  α; `look` and cumulative `alpha_spent` are recorded on the `Comparison`.
- **Adaptive allocation.** Extra trials go to the candidate run only (the baseline is usually a
  stored artifact of `main` that cannot be re-run). Each trial goes to the scenario where it most
  reduces Var(Δ), with flipped scenarios (majority pass on one side, majority fail on the other)
  weighted 2x. Deterministic, so no seed.
- **Flaky.** Pass rate in the closed range [0.2, 0.8], pooled over identical runs.
- **Judge calibration.** Labels join grades on (trial, grader, rubric version), not grade id, so
  a re-score by another judge model reuses the same labels. Precision and recall use **fail** as
  the positive class. Trust uses the point κ (≥ 0.70, as the spec states). Drift is flagged when
  the paired bootstrap interval of the κ change is entirely below 0, or the judge crosses from
  trusted to untrusted.
- **Latency quantiles.** Inverted-CDF quantiles (always an observed value), bootstrap resampling
  whole trials, since turns of one trial are correlated.

## Consequences

- WS7's gate reads `comparisons[0]` for the suite verdict and may show slice verdicts as
  diagnostics; a slice regression with a low raw p but high q is not a regression.
- Every function that resamples takes a seed; per-slice streams are derived from (seed, metric,
  slice key) with SHA-256, so adding a slice or a metric never moves another's numbers.
- A latency comparison between runs (needed for the F7 latency gate) is not part of this ADR; it
  comes with the voice wave.
