# 00rta. Δ's interval when every scenario is all-or-nothing in both runs

Date: 2026-10-24 · Status: accepted · Workstream: red-team P0/P1 fixes (issue #98)

## Context

Δ (candidate minus baseline pass rate) gets a paired cluster bootstrap: resample scenarios, then
each run's trials as Binomial(n, c/n). When every scenario of both runs passed all or none of its
trials, the trial stage is deterministic. If no scenario changed, every replicate is 0 and Δ's
interval is **[0, 0]**, which sits inside [−δ, +δ], so the verdict was NO CHANGE and `hecklr gate`
exited 0 from as little as 1 scenario × 1 trial. ADR 0005c fixed the same collapse for each run's
own pass rate and kept it for Δ on purpose; the red team showed what that costs. With a
deterministic all-pass baseline and a candidate whose true Δ is beyond δ, the verdict was NO
CHANGE 35% of the time at 10 × 1 (Δ = −10 pts), 22% at 33 × 1 (−5 pts) and 14% at 20 × 3 (−3 pts).
All-or-nothing suites are common: `trials: 1`, scripted suites, deterministic agents.

## Decision

When every scenario of both runs is all-or-nothing, each scenario is one pass/fail pair and Δ is
`(gained − lost) / S`: the share of the S scenarios that only the candidate passed minus the share
that only the baseline passed. Its interval combines the Wilson intervals of those two discordant
shares by MOVER, with their multinomial correlation (`paired_difference_interval`), and is
reported with `method: wilson`. Otherwise the paired bootstrap is unchanged. The point estimate,
the sign-flip p-value and the verdict rule are unchanged.

With no discordant scenario the interval is ±(Wilson upper bound of 0 / S): ±56% at S = 3, ±10.4%
at S = 33, ±2.5% at S = 150. "No change" within δ = 3 points from all-passing scenarios therefore
needs about 125 of them. That is the honest answer: 33 scenarios that all passed in both runs
cannot rule out a 5-point regression.

## Consequences

- The issue's cases now give NO CHANGE in 0 of 300 replicates (`test_issue98_*`); measured
  coverage of the interval is ≥ 95% (conservative at small S, nominal by S = 100).
- A deterministic agent on a small suite gets INCONCLUSIVE (exit 2) where it got NO CHANGE. The
  reference agent's scripted A/A, the Action self-test and the fault matrix's clean A/A are now
  inconclusive; SC8 already treats an inconclusive A/A as no false alarm. The Action self-test
  sets `gate.on_inconclusive: pass`, since it checks the exit-code wiring, not the suite's size.
- Mixed suites (some scenarios strictly between 0 and n) keep the bootstrap; the false NO CHANGE
  rate measured there with mostly-boundary scenarios is ≤ 0.4%.
- Supersedes the last consequence of ADR 0005c.
