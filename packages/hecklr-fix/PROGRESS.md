# hecklr-fix progress

Owner: WS15. Spec: `docs/SPEC.md` → "Fixer (`hecklr-fix`)", US-10, WS15 row. Decisions: ADR
0015a (accepted). Schema change RFC 0015a (`agent.command`): deferred to v1.1; `--agent-cmd` is
the v1 mechanism.

## Done

- `patching`: find/replace proposals applied by Hecklr, `git apply`-compatible diffs
  (`test_patching.py`, incl. a hypothesis property and `git apply` round trips).
- `editable`: guardrail, config/world/scenario/persona files refused even if listed
  (`test_guardrail_files_that_define_the_tests_are_never_editable`).
- `target`: mined clusters (ADR 0013a files) and `--from-run` (+ `--baseline`, `--tag`,
  `--scenario`) targets (`test_target.py`).
- `workspace` + `agent`: per-pass project copy, port rewrite, agent process group start/stop
  (`test_workspace.py`, `test_agent.py`, real subprocesses).
- `validation`: cell-paired `compare_runs`, improvement / regression rules, tag-only BH family
  (`test_guard_a_regression_on_one_tag_is_caught_even_when_the_suite_improves`).
- `fixer`, `store`, `report`, `pr` (`test_fixer.py`, `test_report_store.py`, `test_pr.py` with a
  real repo, bare origin and fake `gh`).
- `hecklr fix` CLI (`packages/hecklr/tests/test_cli_fix.py`):
  `test_ws15_fixes_f1_and_the_guard_rejects_a_planted_bad_patch` (rules brain, free, real agent
  processes, `--pr --draft` against a local origin), the mined-cluster "nothing to fix" path and
  exit 78 for every configuration error.
- Coverage of `packages/hecklr-fix/src`: 99% (71 tests).
- Live proof on the LLM brain (`openai/gpt-6-luna`), F1 overlay, `options` scenarios x 3 trials,
  2 candidates, guard `ci` x 1 trial: fix session `01M5K3SFHE8PW4B9MN1TWKACZ5`, outcome
  `fixed`. Candidate 0: targeted 16.7% → 94.4% (+77.8 pts [+55.6, +94.4], p < 0.001, 18 cells),
  guard 92.3% → 92.3% (0.0 pts [−10.3, +10.3], p = 1.000, 39 cells). Spend: Hecklr $0.3422
  (fix $0.3004 + initial run $0.0418), agent ≈ $0.39 (measured $0.0447 initial run; fix passes
  estimated from 627 requests at the measured rate): ≈ $0.73 of the $1.20 cap. The OpenAI credit
  balance ran out during the last pass (candidate 1's guard run); candidate 0's runs had no 429.
- Free proof of the planted bad patch (CLI, rules brain, smoke suite): session
  `01M5K52CRHS9QCPNZC7ND8TMB0`; the patch that restores confirmation but plants F5's refund rule
  improves the targeted set by +100 pts yet is `rejected_guard` on `tag=catches-f5`
  (−100 pts, p < 0.001, q < 0.001, 18 cells) while the suite verdict alone was inconclusive.

- Mined targets: `hecklr fix CLUSTER` reads the store with `hecklr_mine` (`load_clusters`,
  `load_queue`) and passes it to `hecklr_fix.mined_target`, instead of a copy of the miner's file
  layout. `hecklr-fix` does not depend on `hecklr-mine` (kept behind `hecklr[mine]`, spec "small
  default install"); without it the command exits 78 naming the extra. Integration test on real
  `hecklr mine` output:
  `packages/hecklr/tests/test_cli_mine.py::test_us10_hecklr_fix_targets_what_hecklr_mine_wrote`.

## Next

- Detect an agent-side provider outage in a pass (agent errors from a 429 count as failures) and
  refuse the pass, like `scripts/fault-matrix`'s `check_health`.
- v1.1: read `agent.command` from `hecklr.yaml` once RFC 0015a lands with the next schema
  revision (deferred by the architect review, 2026-10-23).

## Open questions

- None blocking.
