# 0005a. Reference suite size: 60 scenarios x 5 trials for SC5 and SC6

Date: 2026-10-21 · Status: accepted · Workstream: WS5

## Context

SC6 requires at least 80% power to detect a 10-point pass-rate drop "on the reference suite", and
SC5 requires a regression false-alarm rate of at most 5% at α = 0.05. Both are properties of the
whole decision procedure (paired sign-flip permutation test, then the verdict rule with δ = 0.03),
so they depend on the suite size and on how heterogeneous the per-scenario pass rates are. WS8
builds the reference agent's suite and needs a number to size it from.

## Decision

**The reference suite has at least 60 scenarios with 5 trials per scenario per run** (300 trials
per run). WS8 sizes the `acme-maintenance` suite from this; more scenarios only add power.

How it was measured (`hecklr_stats.validation.verdict_rate`, seeded):

- Per-scenario true pass rates come from `REALISTIC_SUITE`, a Beta mixture: 50% reliable
  scenarios (Beta(20, 1), mean 0.95), 30% usually fine (Beta(6, 2), mean 0.75), 20% hard or flaky
  (Beta(2, 2), mean 0.50). Suite pass rate is about 0.80. A fresh suite is drawn per replicate.
- The candidate's drop is one logit shift, calibrated so the suite mean falls by exactly 10
  points. "Diffuse" applies it to every scenario; "concentrated" to a random 30% of scenarios (a
  fault that breaks one feature area).
- Each replicate simulates Bernoulli trials for both runs, runs `paired_comparison` and
  `decide_verdict` with α = 0.05, δ = 0.03. 2,000 replicates per cell, 1,000 bootstrap and
  permutation resamples (the regression verdict uses only the permutation p-value and the point
  Δ, so the bootstrap count does not affect it).

| Scenarios x trials | Trials per run | Power, diffuse | Power, concentrated (30%) | A/A regression rate |
| --- | --- | --- | --- | --- |
| 25 x 10 | 250 | 0.796 | 0.529 | 0.021 |
| 30 x 10 | 300 | 0.884 | 0.643 | 0.022 |
| 40 x 10 | 400 | 0.960 | 0.822 | 0.018 |
| 50 x 5 | 250 | 0.843 | 0.698 | 0.019 |
| **60 x 5** | **300** | **0.906** | **0.804** | **0.018** |
| 80 x 3 | 240 | 0.821 | 0.758 | 0.015 |
| 100 x 3 | 300 | 0.913 | 0.874 | 0.025 |

A coarser 400-replicate grid (10 to 80 scenarios x 3, 5, 10 trials) shows the same shape: below
about 250 trials per run, power for a 10-point drop is under 80% for any split.

60 x 5 is the cheapest design measured that reaches 80% for both effect shapes. 100 x 3 is as
cheap per run and slightly more robust to concentrated faults, but 3 trials per scenario makes
pass^k for k > 3 inestimable and per-scenario intervals very wide, so 5 trials is preferred. The
A/A regression rate stays near α/2, as expected for a two-sided test of which only one tail is a
regression; the improvement tail behaves the same.

## Consequences

- `tests/test_validation.py` pins this: `test_sc5_aa_regression_false_alarm_rate_at_most_5_percent`
  and `test_sc6_power_at_least_80_percent_for_a_10_point_drop` run 200 replicates at 60 x 5 on
  every push; `test_too_small_a_suite_is_underpowered` shows the sizing is not slack.
- The power for a concentrated 30% fault is at the 80% line. If the reference suite's own
  faults (F1-F6) turn out more concentrated than that, WS8 should add scenarios, not trials.
- The numbers assume the `REALISTIC_SUITE` heterogeneity. Once the reference agent exists,
  `hecklr calibrate --aa` measures the real false-alarm rate, and this ADR is updated if the real
  pass-rate distribution differs materially.
