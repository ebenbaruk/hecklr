# 0021a. Local CI gate while GitHub Actions is unavailable

Date: 2026-10-23 · Status: superseded by [0022a](0022a-local-ci-is-the-merge-gate.md) · Role: integrator (Wave 4)

## Context

CLAUDE.md and the spec make green CI a merge condition. During Wave 4 every GitHub Actions job on
`ebenbaruk/hecklr` is refused before it starts ("recent account payments have failed or your
spending limit needs to be increased"). That is a billing decision for the repository owner, not
something the build can or should change. Four approved PRs (#77, #78, #79, #80) were waiting on CI
that cannot run, and waiting would stall the wave for a reason unrelated to the code.

Options considered:

1. **Wait for Actions.** Safe but blocks the wave indefinitely on a decision outside the project.
2. **Merge on review alone.** Rejected: "it was reviewed" is a claim, not evidence; main must stay
   green.
3. **Run exactly the CI workflows locally on the tree being merged.** Chosen.

## Decision

Until Actions runs again, the merge gate is a local run of the workflow steps in
`.github/workflows/*.yml`, on the exact tree that will land (the PR branch after merging
`origin/main` into it):

- `ci.yml`: `uv sync --all-packages --all-extras`; `uv run ruff check .`; `uv run ruff format
  --check .`; `scripts/mypy-all.sh`; `uv run pytest -m sc4_timing -p no:xdist` (with
  `HECKLR_SHARED_CPU=1`, as in CI); `uv run pytest -n auto` with coverage ≥ 85%; `uv run pytest
  tests/e2e -m fake`.
- `fault-matrix.yml`: `scripts/fault-matrix/fault_matrix.py --tier free`.
- `ui.yml`: `npm ci`, lint, typecheck, test, build, and the wheel-contains-the-app check.
- `hecklr-action-selftest.yml`: the composite action's run, latest-run and gate logic for the
  baseline, clean, F3 and unusable-agent steps (the GitHub-only steps, cache, artifact upload and
  PR comment, cannot run locally).
- Once the docs site exists: `mkdocs build --strict` and `uv run pytest tests/docs -p no:xdist`.

SC4 timing needs a quiet CPU (ADR 0010b). Load averages are recorded at every step; a timing case
that fails under load is rerun alone once the load is lower, and both results are reported. The
results, with numbers and load averages, are posted on the PR as a "Local CI (Actions blocked by
billing)" comment before it is merged.

## Consequences

- Merges continue with the same checks CI would run, on the merged tree, with evidence on the PR.
- A local machine is not a clean runner: other processes share the CPU, and the macOS toolchain
  differs from `ubuntu-latest`. Platform-specific failures can slip through.
- **When billing is restored, re-run every workflow on `main`** (and on any PR still open) and
  fix anything that fails before further merges. This ADR is then superseded: green Actions is
  the gate again.
