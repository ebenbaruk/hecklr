# hecklr-stats

Hecklr statistics: intervals, paired tests, verdicts, adaptive design. numpy and scipy only,
vectorized, and every resampling call takes an explicit seed.

```python
from hecklr_stats import TrialOutcome, compare_runs, summarize_run

outcomes = [TrialOutcome(scenario_id="reschedule", passed=True, labels={"channel": "chat"})]
summaries = summarize_run(outcomes, run_id=run_id, seed=0, resamples=10_000)
comparisons = compare_runs(base, cand, baseline_run=b, candidate_run=c, config=stats, seed=0)
```

- `summarize_run`: per-scenario Wilson pass rates; suite pass rate, pass^k, pass@k (cluster
  bootstrap); p50/p95/p99 response latency (trial-block bootstrap); per slice.
- `compare_runs`: Δ of per-scenario pass rates, paired cluster bootstrap CI, sign-flip permutation
  p, Benjamini-Hochberg q across slices, verdict, `trials_needed`; optional group-sequential look.
- `allocate_trials`, `group_sequential_boundaries`: adaptive trials with O'Brien-Fleming spending.
- `flaky_scenarios`, `calibrate_judge`, `judge_drift`: flaky detection and judge calibration.
- `verdict_rate`: A/A and power simulations on synthetic suites (SC5, SC6).

Design decisions: `docs/adr/0005a-reference-suite-size-and-power.md`,
`docs/adr/0005b-comparison-decisions.md`. Part of [Hecklr](../../README.md). Owned by WS5.
