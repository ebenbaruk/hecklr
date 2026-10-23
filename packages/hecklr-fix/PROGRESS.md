# hecklr-fix progress

Owner: WS15. Spec: `docs/SPEC.md` → "Fixer (`hecklr-fix`)", US-10, WS15 row. ADR 0015a.

## Plan

1. `patching`: the model proposes find/replace edits; Hecklr applies them to the editable files
   and writes the unified diff itself (exact, `git apply`-able). Edits to anything outside
   `agent.editable`, and editable entries that are scenarios, personas, the world or
   `hecklr.yaml`, are refused.
2. `target`: a fix target is a `FailureCluster` plus its scenarios, from the miner's store
   (ADR 0013a) or from a failing run (`--from-run`, optional `--baseline`, `--tag`, `--scenario`).
3. `workspace` + `agent`: every pass runs in a temp copy of the project with the candidate's files,
   its agent started from the `--agent-cmd` template on a free port (ADR 0015a).
4. `validation`: targeted pass (cluster scenarios, paired by trial cell, improvement verdict),
   guard pass (guard suite, regression verdict on the suite or any tag, BH over tags).
5. `fixer`: the pipeline, budget and the honest outcomes; `store` + `report`: artifacts under
   `<output_dir>/fix/<fix id>/`; `pr`: `--pr` through `git worktree` + `gh`.
6. `hecklr fix` CLI; proofs on acme-maintenance (F1 auto-fix, planted bad patch rejected).

## Done

- Empty package scaffold (Wave 0).

## Next

- Everything in the plan.

## Open questions

- `hecklr.yaml` has no field for the agent's start command (the schema is frozen,
  `extra="forbid"`): `--agent-cmd` for now, RFC 0015a proposes `agent.command`.
