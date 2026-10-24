# Statistics

Agents built on language models are noisy: run the same suite twice against the same agent and the
pass rate moves. A 3-point drop on 20 scenarios × 3 trials is usually that noise, and a team that
reacts to it learns to ignore its tests. Hecklr therefore never prints a bare number: every metric
ships with a confidence interval, and every comparison ends in one of four verdicts whose
false-alarm rate is measured, not assumed.

This page states the exact methods. They live in `hecklr-stats` (numpy and scipy only, every
resampling call seeded) and are decided in ADRs
[0005a](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/0005a-reference-suite-size-and-power.md),
[0005b](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/0005b-comparison-decisions.md),
[0005c](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/0005c-suite-interval-when-scenarios-are-all-or-nothing.md) and
[00rta](https://github.com/ebenbaruk/hecklr/blob/main/docs/adr/00rta-delta-interval-when-scenarios-are-all-or-nothing.md).

## Metrics

| Metric | Definition |
| --- | --- |
| Scenario pass rate | Share of a scenario's trials that passed every success criterion |
| Suite pass rate | Mean of the per-scenario pass rates: every scenario weighs the same, however many trials it ran |
| pass^k | Probability that all k trials of a scenario pass: the reliability customers feel. A 90% pass rate can be a 59% pass^5 |
| pass@k | Probability that at least one of k trials passes: the capability ceiling |
| Latency p50 / p95 / p99 | Response latency quantiles per trial and per run (time to first token in chat, end of caller speech to first agent audio in voice) |
| Cost per trial | Model spend of the simulator and judges |

pass^k and pass@k use the unbiased combinatorial estimators (over all k-subsets of a scenario's n
trials), so they can be estimated whenever n ≥ k.

## Intervals

**One scenario: Wilson score interval.** For x passes out of n trials, with z the normal quantile
(1.96 for 95%):

```text
center = (x + z²/2) / (n + z²)
half   = z / (n + z²) · sqrt( x(n − x)/n + z²/4 )
```

Unlike the textbook `p ± z·sqrt(p(1−p)/n)`, it stays inside [0, 1] and keeps its coverage at small
n and near 0% or 100%: 3 passes out of 3 is **100% [43.9%, 100%]**, not "100%".

**The suite: two-stage cluster bootstrap.** Trials of one scenario are correlated (an agent that
fails a scenario tends to fail it every time), so resampling trials independently would make the
suite look far more certain than it is. Hecklr resamples **scenarios** with replacement, then each
drawn scenario's trials (as a binomial draw at its observed rate, which is exact for pass/fail and
vectorizes), and recomputes the suite metric: 10,000 resamples by default (`stats.bootstrap_resamples`),
seeded. The interval is the percentile interval of the replicates. For pass^k and pass@k the
replicates are recentred on the point estimate to remove the second stage's bias. Measured
coverage at nominal 95%: suite pass rate 95.5-96.8%, pass^3 93-97%.

When every scenario passed all or none of its trials (common for small first runs and for scripted
suites against a deterministic agent), the bootstrap collapses to a zero-width interval. Hecklr then
uses the Wilson interval on the number of scenarios instead: 5 of 5 scenarios passing is
**100% [56.6%, 100%]**, 33 of 33 is **100% [89.6%, 100%]** (ADR 0005c).

**Latency quantiles.** Inverted-CDF quantiles (always an observed value), with a bootstrap that
resamples whole trials, since the turns of one call are correlated.

## Comparing a baseline and a candidate

Comparisons are **paired by scenario**: only scenarios present in both runs count. The suite-level
effect is the mean of the per-scenario differences over the S shared scenarios:

```text
Δ = (1/S) · Σₛ ( p̂ₛ(candidate) − p̂ₛ(baseline) )
```

- **Interval for Δ:** the paired cluster bootstrap above, resampling scenarios with both runs'
  trials, at level 1 − α (so "p < α" and "the interval excludes 0" tell the same story). When
  every scenario of both runs passed all or none of its trials, the bootstrap would collapse to
  [0, 0] whenever nothing flipped; Δ then gets a paired-proportion interval on the scenarios
  instead (the Wilson intervals of the shares that only one run passed, combined by MOVER;
  ADR 00rta). With nothing flipped it is ±10.4 points at 33 scenarios, so "no change" within
  3 points from all-passing scenarios needs about 125 of them.
- **p-value:** a two-sided paired permutation test that flips the sign of each scenario's
  difference. It is exact (all 2^S sign patterns) up to 16 scenarios, and a seeded Monte Carlo
  estimate `(1 + hits) / (1 + B)` above, which keeps the test valid. With 5 or fewer scenarios no
  result can reach α = 0.05 (the smallest possible p is 2/32): Hecklr says so rather than pretending.
- **Slices** (each tag, channel, persona, SNR bucket) are compared the same way, and their p-values
  are corrected together with **Benjamini-Hochberg** across all slices of the comparison and
  reported as q-values. The suite-level comparison is decided on its own p-value: the gate must not
  depend on how many tags a team happens to define.

### The verdict rule

With a minimum effect δ that matters to you (`stats.min_effect`, default 3 points) and α
(`stats.alpha`, default 0.05):

| Verdict | Condition |
| --- | --- |
| **Regression** | p < α and Δ < −δ |
| **Improvement** | p < α and Δ > +δ |
| **No change** | the whole interval for Δ lies inside [−δ, +δ] |
| **Inconclusive** | anything else |

An inconclusive verdict says how many more trials per scenario would likely decide it
(`trials_needed`): assuming the observed Δ is the truth and the standard error shrinks with
1/√trials, the count that gives an 80% chance of a decision. It is a lower bound when scenarios
disagree with each other, and absent when more trials cannot help (too few scenarios: add
scenarios instead).

### A worked example

The [demo](https://github.com/ebenbaruk/hecklr/blob/main/demo/RESULTS.md) changes one line of the
reference agent's prompt and gates the scripted suite (33 scenarios, 1 trial each):

```text
Pass rate 100.0% [89.6%, 100.0%] → 72.7% [55.8%, 84.9%]:
−27.3 pts [−44.2, −11.2] (95% CI), p=0.004 · 33 scenarios · δ=3 pts, α=0.05
Gate: REGRESSION (exit 1)
```

Nine scenarios flipped from pass to fail, so the sign-flip test asks how often 9 random signs out of
9 non-zero differences all come out negative: 2 · (1/2)^9 ≈ 0.004 (with 33 scenarios Hecklr
estimates it by seeded Monte Carlo, which lands on the same value). The interval lies entirely below
−δ, and p < α: a regression.

## Adaptive trials

With `--adaptive`, after the planned trials Hecklr spends up to 50% more on the scenarios where an
extra trial most reduces the variance of Δ, weighting scenarios that flipped against the baseline
twice. Extra trials go to the candidate only (the baseline is usually a stored run of `main` that
cannot be re-run).

Looking at the data more than once inflates false alarms unless each look spends only part of α.
Hecklr uses a group-sequential design with the Lan-DeMets **O'Brien-Fleming** alpha-spending
function, α/2 per side, so its boundaries match published tables (5 looks: z = 4.877, 3.357, 2.680,
2.290, 2.031). Each look compares the permutation p-value with that look's nominal α, and records
the look number and cumulative α spent on the comparison. In a 400,000-path simulation the overall
false-alarm rate stays at 0.050.

## How the statistics are validated

These are tests in CI, not claims:

| Property | Target | Measured |
| --- | --- | --- |
| A/A false-alarm rate (SC5) | ≤ 5% of 200 comparisons at α = 0.05 | 1.8% over 2,000 replicates (60 scenarios × 5 trials) |
| Power for a 10-point drop (SC6) | ≥ 80% on the reference suite size | 90.6% (drop spread over all scenarios), 80.4% (drop concentrated on 30% of them) |
| Δ interval coverage | 95% | ≥ 92% |
| Group-sequential false alarms | α = 0.05 overall | 0.050 |

The power analysis is also why the reference suite has **at least 60 scenarios × 5 trials**:
below about 250 trials per run, no split of scenarios and trials reaches 80% power for a 10-point
drop (ADR 0005a). Size your own gating suite the same way: more scenarios buy more power than more
trials, because between-scenario variation does not shrink with trials.

On your own agent, `hecklr calibrate --aa` runs the same agent twice and reports whether the
comparison stays quiet, plus a simulated false-alarm rate at your suite's size.

**Flaky scenarios.** A scenario whose pooled pass rate across identical runs sits between 20% and
80% is flagged as flaky, with its interval, so you fix the scenario or accept it knowingly. Runs
are identical when they share the suite, the agent (name and version) and the recorded commit;
`hecklr compare`, `gate`, `report`, the PR comment and `hecklr calibrate --aa` (whose two runs are
identical by construction) then flag them. Runs without a recorded commit are never treated as
identical, because a real change pooled over both runs would look flaky.

**Judges.** Cohen's κ of every judge rubric against human labels, with an interval; rubrics below
κ = 0.70 are marked untrusted ([Graders and judges](graders.md#llm-judges)).
