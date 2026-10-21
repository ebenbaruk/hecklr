# 0001. Monorepo with one package per component, frozen schemas at the root

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

Hecklr is built by up to six parallel builder agents (spec → "Agent swarm build plan"). Parallel
work only stays coherent if no two builders own the same directory and every shared type is fixed
before Wave 1 starts. Users also need a small default install: audio (aiortc, ONNX, faster-whisper)
and mining (Presidio, HDBSCAN) pull heavy native dependencies most chat users never need.

## Decision

- One `uv` workspace, Python 3.12, a single `uv.lock`. The root is virtual (no `[project]`): it holds
  shared ruff, mypy, pytest and coverage configuration plus the `dev` dependency group.
- 14 packages under `packages/`, one per component in the spec's repo layout, each with a src layout,
  `py.typed`, its own tests and `PROGRESS.md`. Import names are `hecklr_<name>`; the meta-package
  is `hecklr` and owns the CLI.
- Dependency rule: every package may depend on `hecklr-schemas`; packages that call models depend on
  `hecklr-llm`. Nothing else depends sideways in Wave 0. Components talk through the Protocols in
  `hecklr-schemas` and are wired together by `hecklr-core` and the `hecklr` meta-package, so a
  builder never needs another builder's unmerged code (they use fakes until it lands).
- `hecklr` installs the text-only stack by default; `[voice]`, `[local-voice]`, `[mine]`, `[ui]` and
  `[all]` add the rest.
- `hecklr-schemas` is frozen after Wave 0. Changes go through `docs/rfc/` and the architect.
- Each package has its own `tests/` and `conftest.py`. pytest runs with `--import-mode=importlib`,
  and mypy runs once per package (`scripts/mypy-all.sh`), because identical module names across
  packages would otherwise collide.

## Consequences

- Builders get isolated directories and a stable contract; integration conflicts are limited to
  `uv.lock`, which is regenerated rather than merged by hand.
- 14 `pyproject.toml` files to keep consistent; the workspace sources table in the root keeps
  inter-package references in one place.
- Per-package mypy runs cost a few extra seconds in CI, in exchange for no test-module renaming.
