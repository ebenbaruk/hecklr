# Contributing to Hecklr

Thanks for helping. Hecklr tests other people's agents, so its own bar is high: every behavior is
tested, every number ships with a confidence interval, and `main` is always green.

## Setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you) and git.

```bash
git clone https://github.com/ebenbaruk/hecklr && cd hecklr
uv sync --all-packages --all-extras
uv run pre-commit install
uv run hecklr version
```

## Checks (all must pass before you push)

```bash
uv run ruff check . && uv run ruff format --check .
scripts/mypy-all.sh                      # mypy --strict on every package
uv run pytest -n auto                    # unit tests; never touch the network
uv run pytest tests/e2e -m fake          # end-to-end on FakeLLM
```

Tests that call a real model are marked `@pytest.mark.live`, excluded by default, and capped by
`HECKLR_TEST_BUDGET_USD`. Run them with `uv run pytest -m live`. A local
[Ollama](https://ollama.com) (`ollama serve`) is enough for most of them.

## Ground rules

- Read the relevant section of [`docs/SPEC.md`](docs/SPEC.md) first. If code and spec disagree,
  the spec wins; propose a spec change rather than diverging.
- `packages/hecklr-schemas` is frozen. Changing it needs an RFC in `docs/rfc/`.
- Every random choice takes an explicit seed. Times are integer milliseconds on the harness
  monotonic clock. IDs are ULIDs.
- Record architectural decisions as ADRs in `docs/adr/`.

## Branches, commits and pull requests

- Branches: `ws-<n>-<slug>` for workstreams, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/), e.g.
  `feat(stats): add wilson interval`. Each commit builds and passes tests.
- Pull requests: one coherent slice, under ~600 changed lines; describe what, why, how you tested
  it, and the spec IDs (SC*, US-*) covered. Squash-merge after green CI and an approving review.
