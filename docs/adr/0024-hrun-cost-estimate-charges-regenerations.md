# 0024-hrun. The cost estimate charges simulator regenerations and prints rounded up

Date: 2026-10-24 · Status: accepted · Workstream: WShrun (issues #85, #84) · Amends: ADR 00hca

## Context

ADR 00hca made the pre-run estimate an upper bound on judge spend. The simulator side still
charged one model call per customer turn, but `hecklr_sim`'s guided simulator regenerates a draft
that its consistency guard rejects (or that is not valid JSON) up to `max_regenerations` times,
2 by default, so a turn can cost three calls (#85). And `hecklr run` printed the estimate rounded
to the nearest cent, so an estimate of $0.0149 read "at most $0.01" (#84).

## Decision

- **Every draft is charged.** A customer turn costs `SIM_DRAFTS_PER_TURN` = 3 calls, each reading
  the brief and the dialogue so far, and each regeneration also its feedback
  (`SIM_FEEDBACK_TOKENS` = 200). `hecklr run` builds its simulators with `hecklr_sim`'s defaults
  (2 regenerations, no LLM consistency check), which
  `test_issue85_the_estimate_charges_the_simulator_hecklr_run_builds` checks, since hecklr-core
  does not import hecklr-sim.
- **The output of a draft stays charged at 80 tokens** (a customer message), not at the
  simulator's `max_tokens` of 2,048. Charging `max_tokens` would multiply the simulator estimate by
  about six on acme-maintenance with gpt-6-luna (`ci`: $4.28 instead of $0.74), far beyond any
  measured run (variants of the fault matrix billed $0.15 to $0.33), and the fault matrix's
  per-variant budgets would refuse every live variant. The guarantee against overspending is the
  hard cap during the run (spec, "Run lifecycle"), which counts real billed tokens; the estimate
  is the early refusal. For a reasoning model that spends many output tokens per message, the
  estimate is an upper bound only under this per-message assumption.
- **The printed figure rounds up**: to the cent, or to a hundredth of a cent below one cent
  (`hecklr.commands.run.at_most_usd`), so "at most" never prints less than the estimate.

## Consequences

- Guided suites estimate about three times higher on the simulator side (acme-maintenance with
  luna: `ci` $0.74 + $0.76 judges, `data-protection` $0.34 + $0.12, `matrix-live` $1.09 + $0.99).
  Scripted suites are unchanged (no simulator call).
- A budget set just above a past run's estimate may now be refused; raise `--budget-usd`.
