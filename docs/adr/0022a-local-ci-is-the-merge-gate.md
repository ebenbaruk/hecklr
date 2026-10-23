# 0022a. Local CI is the merge gate; GitHub Actions is off on the private repo

Date: 2026-10-23 · Status: accepted · Supersedes: [0021a](0021a-local-ci-gate-while-actions-unavailable.md)

## Context

ADR 0021a ran the CI workflows by hand while GitHub Actions refused jobs for billing reasons, as a
stopgap until billing came back. The repository owner decided not to pay for Actions minutes on
the private repository. Actions is not what makes the checks trustworthy: every check is a
command (`ruff`, `mypy`, `pytest`, the fault matrix, MkDocs, the UI build, the action self-test)
that runs the same on a laptop. GitHub-hosted runners are free for public repositories, so the
question returns at launch.

## Decision

- **The merge gate is `scripts/ci-local.sh`**, run on the exact tree that will land (the PR
  branch with `origin/main` merged in). It runs every step of `.github/workflows/*.yml` and
  exits non-zero if any fails. `scripts/ci-selftest.sh` replaces the action self-test job.
- The PR gets a "Local CI" comment with the summary (`.ci-local/summary.txt`: steps, exit codes,
  load averages) before it is merged. A failing step blocks the merge as a red CI check would.
- GitHub Actions is **disabled** on `ebenbaruk/hecklr` (repository setting), so no job starts
  and nothing is billed. The workflow files stay: they document the checks, they are the
  examples users copy for the Hecklr Action, and they run for free once the repository is public.
- A timing-sensitive failure under load (SC4 timing, ADR 0010b, or a subprocess start-up
  deadline) is rerun alone on a quieter machine, an issue is filed, and both results are
  reported. Any other failing step blocks the merge.

## Consequences

- No cost, and the same checks as before, on the merged tree.
- A laptop is not a clean runner: other work shares the CPU and macOS differs from
  `ubuntu-latest`, so Linux-only failures can slip through. When the repository goes public,
  re-enable Actions, run every workflow on `main`, and fix what fails; this ADR is then
  superseded and green Actions is the gate again.
- Nothing enforces the gate mechanically (no required status check on GitHub Free). The PR
  comment is the record; a merge without one is a process violation.
