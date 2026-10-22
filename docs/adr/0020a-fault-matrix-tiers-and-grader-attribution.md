# 0020a. Fault matrix: three tiers, judge-only twins and per-grader attribution

Date: 2026-10-22 · Status: accepted (orchestrator in the architect role, 2026-10-22, PR #72) · Workstream: WS-m1 (milestone M1)

## Context

SC8 says each seeded fault must produce "a regression verdict on the expected metric" and the
clean agent none against itself. Gate M1 needs that proven for F1-F6 on the chat reference agent,
reproducibly, every night for free and on real models within a $6 budget. Three gaps showed up:

- `hecklr gate` decides on the suite pass rate. It does not say which grader caught a fault, and
  a fault caught only by a backstop (F3's `not_contains: €`) would pass for "caught" although the
  spec names the `no_hallucination` judge.
- The free, deterministic matrix (rules brain, scripted `smoke` suite, ADR 0008a) has no judges,
  so F1's `resolved`, F3's `no_hallucination` and F6's `handoff_appropriate` were never measured
  on it.
- The agent's own model calls (Tier B) are invisible to Hecklr's `--budget-usd`.

## Decision

- **Three tiers**, one script (`scripts/fault-matrix/fault_matrix.py`), each running the clean
  agent twice (baseline, then A/A with root seed + 1) and F1-F6 once, all over the `http` adapter:
  - `free`: rules brain, `smoke` suite, deterministic graders only; no model, no key. Nightly CI.
    Its expected graders are the spec's where deterministic, else the scenarios' deterministic
    backstop (F3 `not_contains`, F6's `tool_called` in the `tool` family).
  - `a`: rules brain, `matrix` suite = `smoke` + 18 **judge-only twins** (`scripted/judged/`: the
    same scripted customer, one judge criterion). Real judges; expected graders = the spec table.
  - `b`: LLM brain, guided customers and judges on a real model (`matrix-live`: core + booking +
    red-team scenarios, 3 trials). Expected graders = the spec table.
- **Per-grader attribution.** Besides `hecklr gate`'s suite verdict, every grader family
  (`judge:<rubric>` or the criterion type) gets its own paired comparison with `hecklr_stats`
  (`compare_runs`, same α, δ, bootstrap and sign-flip test) on the scenarios that carry it: a
  trial passes for that grader unless the grader failed (or the trial did not end normally).
  A fault counts as **caught** only when the gate exits 1 **and** every expected grader's own
  verdict is a regression. The A/A row must not exit 1 or 3; an inconclusive A/A is not a false
  alarm (the spec's false-alarm criterion is about regression verdicts).
- **The Tool grader is one family.** The spec's Graders table lists `tool_called`,
  `tool_not_called`, `args_match` and `call_order` as one family, "Tool"; the attribution counts
  them as one grader, `tool`, and F1's expected `tool_called` is read as that family. On a strong
  model the F1 agent books the first free slot, the customer objects and the agent rebooks the
  slot they chose: `tool_called` finds the corrected call and passes, and only `args_match`
  ("every booking call has the chosen slot") sees the unconfirmed booking. The six options-first
  scenarios carry both.
- **F1 replaces the confirmation rule with its opposite** instead of deleting it. Deleted, the
  rule changed nothing on `gpt-6-luna`, which offers slots and asks for a yes by default (Tier B
  pilot: no tool, state or judge regression); a fault must change behaviour to be seeded. The
  overlay is still a one-line prompt change, like F3-F6.
- **Judge-only twins** rather than judge criteria on the smoke scenarios: `smoke` must stay free
  and model-less, and a twin isolates the judge's verdict from the deterministic graders.
- **Budget.** One hard cap split in two pools: Hecklr's (`hecklr run --budget-usd`, cumulative)
  and the agent's (`serve.py --budget-usd`, read back from `/healthz spent_usd`). The baseline run
  is the cost pilot: if every run costs what it did and the tier would exceed the cap, the script
  stops before spending more. The agent checks its cap before each model call and counts the
  cost after it, so concurrent trials can overshoot its pool by at most one completion each.
- The script copies the suite files into its output directory with a derived `hecklr.yaml`
  (port, private store, concurrency), so a run never writes into the repository and the copy
  records exactly what was tested.

## Consequences

- With 6 flipped scenarios per fault the smallest attainable sign-flip p is 2/64 ≈ 0.031 (ADR
  0008a); the free and `a` tiers sit there for F2, F4 and F5. A single judge flip on an unrelated
  scenario of the same run can push the *suite* p above 0.05 (seen before the world got its
  calendar); more scenarios per fault would add margin.
- Per-grader verdicts are diagnostics with no multiplicity correction; only the expected graders
  of each row are pass/fail criteria.
- The free tier's A/A is clean by construction: the rules brain and the scripted customers are
  deterministic, so its Δ is exactly 0 and it says nothing about the false-alarm rate. Tier A's
  A/A only re-samples the judges; Tier B's A/A (guided customers, LLM agent, judges) is the one
  that exercises SC5 on this agent.
- The free tier passes a placeholder `OPENAI_API_KEY` when none is set (issue #19); it can never
  spend, and the script fails the tier if anything was spent.

## Spec change (applied on acceptance)

Fault table (spec, "Fault-injection matrix (SC8)"), so the spec says what the matrix tests:

| # | Fault | Must be caught by |
| --- | --- | --- |
| F1 | Prompt tells the agent to book the first free slot without confirming it (dropping the confirmation rule alone does not change a strong model's behaviour) | Tool grader (`tool_called`, `args_match`) + `resolved` judge |

## Acceptance review

Checked against SPEC.md, "Fault-injection matrix (SC8)" and "Graders":

- The Tool reading is the spec's own: its Graders table lists `tool_called`, `tool_not_called`,
  `args_match` and `call_order` as one family, "Tool". `args_match` is what sees F1's
  unconfirmed booking once a strong model self-corrects, so naming it keeps F1 measurable without
  a new grader or a looser threshold.
- The F1 overlay is still a one-line prompt change in `examples/acme-faults/`, as the spec's
  "each variant changes one thing" requires. A fault that does not change behaviour cannot be
  caught by any grader, so seeding the opposite rule is the honest variant.
- "Caught" is stricter than SC8's wording (suite gate *and* every expected grader must regress),
  and the A/A rule matches SC8's "no regression verdict". Neither loosens a criterion.
- The tiers add measurements; Tier B on real models remains the SC8 evidence. The free tier's
  backstop graders (F3 `not_contains`, F6 `tool`) are diagnostics for nightly CI, not a substitute for the
  spec's judges.
