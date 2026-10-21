# 0005c. Suite intervals when every scenario passed all or none of its trials

Date: 2026-10-22 · Status: accepted · Workstream: WS-wire (issue #14, WS5's package)

## Context

Suite metrics (pass rate, pass^k, pass@k) are the mean of a per-scenario statistic with a
two-stage cluster bootstrap (spec, "Intervals"; ADR 0005a). The trial stage draws each scenario's
passes from Binomial(n, c/n). When a scenario passed all or none of its trials, that draw is
deterministic. When *every* scenario is like that, the only randomness left is the scenario
stage, which is then a percentile bootstrap of a proportion of S Bernoulli outcomes:

- at 0% or 100% every resample equals the point, so the interval is `[100%, 100%]` (issue #14:
  5 scenarios x 1 trial, all passing, printed "100% [100%, 100%]", false certainty at n = 5);
- near the boundary at small S it behaves like a Wald interval and undercovers.

This is common, not a corner case: small first runs, `trials: 1` suites, scripted smoke suites
against a deterministic agent (the reference agent's clean run passes 33/33).

## Decision

When every scenario's observed pass count is 0 or n, `suite_estimate` reports the Wilson score
interval for `x / S`, where S is the number of scenarios and x the number whose statistic is 1
(pass rate, pass^k and pass@k are all exactly 0 or 1 then), with `method: wilson` and `n = S`.
Otherwise the cluster bootstrap is unchanged.

Wilson on the scenario count is the interval the spec already uses for proportions at small n
and near 0 or 1, applied to the unit that is actually independent (the scenario; trials of one
scenario are correlated, and here perfectly so). No minimum suite size or ad-hoc floor is needed:
at S = 5 all passing it gives [56.6%, 100%], at S = 33 [89.6%, 100%].

## Consequences

- `hecklr run` and reports never show a zero-width suite interval; the `method` field says which
  interval was used.
- Coverage of the fallback, deterministic scenarios passing with probability 0.9 at S = 5, 10,
  20: >= 90% in `test_all_or_nothing_suite_interval_covers_at_nominal_rate` (Wilson's known
  near-nominal coverage). The bootstrap's own calibration tests (suite pass rate, pass^3) and the
  SC5/SC6 validations are unaffected: realistic suites always have a scenario off the boundary.
- The rule is discontinuous: one scenario at 2/3 switches back to the bootstrap. That case has
  real within-scenario variance for the bootstrap to see, and both intervals are valid there.
- Comparisons (Δ, p-values, verdicts) are unchanged; an all-pass baseline and candidate already
  yield "no change" through the paired bootstrap's zero-width Δ interval inside [−δ, +δ].
