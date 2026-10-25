# Contributing to Hecklr

Thanks for helping. Hecklr tests other people's agents, so its own bar is high: every behavior is
tested, every number ships with a confidence interval, and `main` is always green. This guide gets
you from a clone to a merged pull request.

New here? Issues labeled
[`good first issue`](https://github.com/ebenbaruk/hecklr/labels/good%20first%20issue) are scoped
to one package, with the files to touch and how to test them. Comment on one before you start so
nobody duplicates your work. By participating you agree to the [code of conduct](CODE_OF_CONDUCT.md).

## Development setup

Requirements: [uv](https://docs.astral.sh/uv/) (it installs Python 3.12 for you) and git. Docker
is needed only for the LiveKit and SMTP/IMAP tests (they skip, saying why, without it); Node.js
(20.19 or newer; CI uses 24) only for the UI; `ffmpeg` only for the AMR-NB and Opus phone-line
codecs.

```bash
git clone https://github.com/ebenbaruk/hecklr && cd hecklr
uv sync --all-packages --all-extras
uv run pre-commit install
uv run hecklr version
npm --prefix ui ci && npm --prefix ui run build   # the UI (the first `hecklr ui` does it otherwise)
```

The built UI lands in `packages/hecklr/src/hecklr/ui_api/static/` (git-ignored). Rebuild it after
changing or pulling `ui/`, or work on it with `hecklr ui --dev` and `npm run dev` in `ui/`. UI checks:
`npm --prefix ui run lint`, `npm --prefix ui run typecheck`, `npm --prefix ui test`.

Try the whole pipeline on the reference agent, free and offline:

```bash
uv run python examples/acme-maintenance/agent/serve.py --brain rules --port 8000 &
cd examples/acme-maintenance && uv run hecklr run --suite smoke
```

## Repository layout

| Path | What it is |
| --- | --- |
| `packages/hecklr-schemas` | The frozen data model and protocols every package shares |
| `packages/hecklr-core` | Orchestrator: matrix, seeds, budget, cache, storage |
| `packages/hecklr-llm` | Model providers, the response cache and `FakeLLM` for tests |
| `packages/hecklr-sim` | The simulated customer, including the adversarial pack |
| `packages/hecklr-world` | The world sandbox (MCP + HTTP) |
| `packages/hecklr-channels`, `hecklr-voice`, `hecklr-adapters` | Chat and email drivers, the voice engine, the transports to agents |
| `packages/hecklr-grade`, `hecklr-stats`, `hecklr-report` | Graders and judges, statistics, report formats |
| `packages/hecklr-mine`, `hecklr-fix` | The miner and the fixer |
| `packages/hecklr` | The CLI, the Python API, the pytest plugin and the UI's API |
| `ui/` | The React UI |
| `action/` | The GitHub Action |
| `examples/` | Reference agents (chat, voice, email), the loopback calibration agent and the 12 seeded faults |
| `docs/` | The docs site (MkDocs), the spec, ADRs, RFCs and milestone reports |
| `tests/e2e`, `tests/docs` | Cross-package end-to-end tests and the executed docs |

Each package has a `README.md` (its public API) and a `PROGRESS.md` (what is done and next).

## Checks (all must pass before you push)

```bash
uv run ruff check . && uv run ruff format --check .
scripts/mypy-all.sh                      # mypy --strict on every package
uv run pytest -n auto                    # unit tests; sockets are blocked (pytest-socket)
uv run pytest -m sc4_timing -p no:xdist  # SC4 voice timing, serially on an idle CPU (ADR 0010b)
uv run pytest tests/e2e -m fake          # end-to-end on FakeLLM
```

Coverage of every core package stays at or above 85% (`--cov=packages/<pkg>/src`). Any
`# type: ignore` or `# noqa` needs a comment saying why.

Tests that call a real model are marked `@pytest.mark.live`, excluded by default, and capped by
`HECKLR_TEST_BUDGET_USD`. Run them with `uv run pytest -m live`; they are the only tests allowed
to open network sockets. A local [Ollama](https://ollama.com) (`ollama serve`) is enough for most
of them. Everything else uses `FakeLLM` or recorded responses.

The SC4 timing tests measure real time. Under parallel load (`-n auto`, or other work on your
machine) they are skipped or may fail; run them serially on an idle machine before calling a voice
change done.

### The merge gate

GitHub Actions is off on the private repository ([ADR 0022a](docs/adr/0022a-local-ci-is-the-merge-gate.md)).
Before a pull request is merged, run every CI workflow locally on the tree that will land (your
branch with `origin/main` merged in) and paste `.ci-local/summary.txt` into a "Local CI" comment:

```bash
scripts/ci-local.sh            # everything in .github/workflows/, about 20 minutes
scripts/ci-local.sh --quick    # lint, types, tests, e2e: for iterating, not for merging
```

## Writing tests

- Test first for behavior named in the spec's success criteria (SC1-SC10) or user stories
  (US-01-US-12), and cite the id in the test name: `test_sc4_latency_within_20ms`.
- Every random choice takes an explicit seed (`numpy.random.default_rng(seed)`,
  `random.Random(seed)`); never global random state.
- Times are integer milliseconds on the harness monotonic clock; ids are ULIDs.
- Audio tests use generated signals; nothing downloads models in unit tests.
- Fix root causes. Never weaken, skip or delete a test to get green, and never loosen a statistical
  threshold without an ADR.

## Documentation

The docs site lives in `docs/` and builds with MkDocs Material:

```bash
uv sync --all-packages --all-extras --group docs
uv run --group docs mkdocs serve                 # http://127.0.0.1:8000/
uv run --group docs mkdocs build --strict        # what CI runs
uv run python docs/scripts/gen_reference.py      # regenerate the CLI and configuration reference
uv run pytest tests/docs                         # the executed docs, SC1 included
```

Docs cannot rot: `tests/docs` checks that every `hecklr` command line in the docs parses against
the CLI, that every YAML block titled `hecklr.yaml`, `scenarios/...`, `personas/...` or
`worlds/...` validates against the schema, that the generated reference pages match the code, and
it runs the quickstart literally (SC1), on the starter agent and on the reference agent. The
command check runs each documented line with `--help` appended, so it catches unknown commands and
options, not wrong option values: run a new example for real once. If you change a command, a flag or a config field, update
the docs in the same pull request; CI tells you where.

## Decisions: ADRs and RFCs

- [`docs/SPEC.md`](docs/SPEC.md) is the source of truth. If code and spec disagree, the spec wins;
  if you think the spec is wrong, propose a change in an issue rather than diverging.
- **ADRs** (`docs/adr/NNNN-title.md`) record real architectural decisions: context, decision,
  consequences, one page. Add one in the pull request that makes the decision.
- **RFCs** (`docs/rfc/NNNN-title.md`) are required for any change to `packages/hecklr-schemas`,
  which is frozen. Describe the change, why it is needed, and its migration; the architect role
  approves it before any code changes.

## Branches, commits and pull requests

- Branches: `ws-<n>-<slug>` for workstreams, `fix/<slug>`, `chore/<slug>`, `docs/<slug>`.
- Commits follow [Conventional Commits](https://www.conventionalcommits.org/), e.g.
  `feat(stats): add wilson interval`; each commit builds and passes tests. The message says why
  when it is not obvious.
- Pull requests: one coherent slice, under ~600 changed lines (generated code excepted). Describe
  what, why, how you tested it (commands and results), and the spec ids (SC*, US-*) covered.
- CI must be green and a reviewer must approve with no blocking comments. We squash-merge and
  delete the branch.
- Never commit secrets (keys live in `.env`, which is git-ignored, or the environment), large
  binaries, or generated audio (tests generate their own).

## Reporting bugs and security issues

Use the issue templates for bugs and feature requests. Report security problems privately, as
described in [SECURITY.md](SECURITY.md).
