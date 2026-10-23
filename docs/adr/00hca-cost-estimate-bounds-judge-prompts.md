# 00hca. The cost estimate bounds what judges are really sent

Date: 2026-10-23 · Status: proposed · Workstream: WShcore (issue #76) · Amends: ADR 0001a ("Cost
estimate")

## Context

`hecklr run` prints "estimated cost at most $X" and refuses to start when X exceeds the budget
(spec, "Run lifecycle"). On the acme `matrix` suite (51 trials, 18 judged on `openai/gpt-6-luna`)
it printed $0.01 (exactly $0.0071) and the run billed $0.0183, so a budget set from the estimate
would have stopped the run partway. Reproduced offline: the 18 judge prompts total 445,000
characters, against the 18 x 1,500 + transcript tokens the planner assumed. The causes:

1. **World snapshots.** `resolved`, `no_hallucination`, `policy_compliance` and `pii_leak` judges
   read the world before and after the conversation (spec: `no_hallucination` checks claims against
   ground truth). acme's world renders to 15,000 characters, twice per prompt. The planner had a
   fixed 1,500 tokens for "rubric, scenario, world and tool results".
2. **Transcript lines.** A judge reads event ids, times, tool calls and tool results, up to about
   1,000 characters per turn on acme, not the 80 tokens per message the simulator sees.
3. **Token density.** JSON and ULIDs bill at about 2.6 characters per token on luna, not 4.
4. **Uncounted judges.** `injection_resisted` and `custom` graders (the adversarial pack's
   `no_pii_leak` and `no_policy_override`) ask the judge model but were not charged.
5. **Retries.** An unusable judge answer is asked again once (issue #37), resending the prompt.

## Decision

The estimate is a true upper bound on judge spend. Per call that asks a model (`judge` votes,
`injection_resisted`, every `custom` criterion):

- prompt = 4,000 characters of instructions (system prompt, longest rubric, answer schema, headings,
  retry note) + the scenario's goal and facts + `max_turns` x 1,200 transcript characters, plus,
  unless the rubric is world-blind (`tone`, `handoff_appropriate`, `injection_resisted`), both world
  snapshots measured from the loaded world (`world_snapshot_chars`) and 400 characters of new
  records per turn;
- tokens = characters / 2;
- each vote is asked twice (the retry resends the first answer), and each answer may reach
  `max_tokens` (1,024), the most a judge can be billed for.

`plan_run` passes the project's world to `estimate_cost`; a world whose seed files were not inlined
cannot be sized and is refused (fail closed, like unknown prices). hecklr-core still does not
import hecklr-grade: `examples/acme-maintenance/tests/test_cost_estimate.py` checks the constants
against the real rubrics and retry count, and runs the issue's 51-trial matrix with a judge billed
at luna's price and density, with the budget set to the estimate: it must finish, and it does.

Harness-error retries are not charged: a retried trial replays its model calls from the response
cache (keyed by model, prompt and seed). The simulator's estimate is unchanged; its draft
regenerations (up to 2 per turn, on a contradiction) are tracked separately (#85).

## Consequences

- "At most" holds for judged suites. It is loose: the matrix estimate is now $0.096 against $0.018
  billed, mostly from the retry and `max_tokens` allowances, which typical runs never use. A budget
  that only just fits a run's real spend can now be refused; raise `--budget-usd` (the hard cap
  during the run is unchanged).
- Estimates on acme with luna: `ci` $0.99, `matrix-live` $1.33, `redteam` $1.08. The fault matrix's
  tier b gives each run what is left of a $4.20 pool, so its last variants may now be refused at the
  default budget; `--resume` with a larger `--budget-usd` finishes them.
