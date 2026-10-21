# Hecklr GitHub Action

`hecklr/action@v1` runs a Hecklr suite on every pull request, compares it with the baseline run
of your default branch, posts **one sticky PR comment** (under 30 lines: verdict, Δ with its
confidence interval, scenarios that flipped grouped by cause) and fails the check only when the
regression is statistically significant and larger than your minimum effect (US-04, US-05).
See `docs/SPEC.md` → "GitHub Action".

```yaml
# .github/workflows/hecklr.yml
on:
  pull_request:
  push:
    branches: [main]

permissions:
  contents: read
  actions: read          # download the baseline run artifact
  pull-requests: write   # the sticky comment

jobs:
  hecklr:
    runs-on: ubuntu-latest
    steps:
      - uses: actions/checkout@v7
      # start your agent here if hecklr.yaml points at a URL
      - uses: hecklr/action@v1
        with:
          suite: ci
          budget-usd: "2"
        env:
          OPENAI_API_KEY: ${{ secrets.OPENAI_API_KEY }}
```

## How it works

1. Installs Hecklr (`install`, default `uv tool install hecklr`). Hecklr is not published on PyPI
   yet, so until a release exists set `install` and `hecklr` yourself (for example
   `install: uv sync --all-packages` and `hecklr: uv run hecklr` in a checkout of this repository,
   as the self-test does); the default would install an unrelated package or fail.
2. On a pull request, downloads the newest `hecklr-baseline-<suite>` artifact uploaded from
   `baseline-branch` into `.hecklr/runs/` (`hecklr github fetch-baseline`). `baseline-run` skips
   this and names a run already in the store.
3. `hecklr run --suite <suite> --baseline <run>`, restoring the LLM response cache first, then
   finds the run it recorded (`hecklr github latest-run --since <start of the step>`).
4. `hecklr gate --candidate <that run>`, which writes `report.md`, `report.html`, `report.json` and `junit.xml` to
   `hecklr-report/`; they are uploaded as the `hecklr-report-<suite>` artifact, and the Markdown
   is added to the job summary.
5. On a pull request, creates or updates the one comment marked `<!-- hecklr-gate:<suite> -->`.
6. On a push to `baseline-branch`, a completed run is uploaded as the new
   `hecklr-baseline-<suite>` artifact (kept 90 days).
7. Exits with the gate's code, or 3 when `hecklr run` failed: a run that exits non-zero (a missing
   key, a bad config, a budget refusal, a crash) never passes, and a job that recorded no run is
   never gated against the fetched baseline.

| Exit | Meaning |
| --- | --- |
| 0 | pass: no change, an improvement, or (with `on-inconclusive: pass`) inconclusive |
| 1 | regression: p < α and Δ < −δ |
| 2 | inconclusive (configurable with `on-inconclusive` or `gate.on_inconclusive`) |
| 3 | harness error: `hecklr run` failed or recorded no run, nothing could be analyzed, or the runs could not be read |

With no baseline yet (the first PR before the default branch ever ran the action),
`missing-baseline: pass` (the default) reports the run alone and passes; `error` exits 3.

## Inputs

| Input | Default | |
| --- | --- | --- |
| `config` | `hecklr.yaml` | relative to `working-directory` |
| `suite` | `ci` | suite to run and gate |
| `working-directory` | `.` | the Hecklr project |
| `baseline-branch` | `main` | where baselines come from and are stored |
| `baseline-run` | | compare with this stored run instead |
| `budget-usd`, `trials` | | override the config |
| `on-inconclusive` | | `fail` or `pass`; empty uses `gate.on_inconclusive` |
| `missing-baseline` | `pass` | `pass` or `error` |
| `comment` | `true` | post the sticky comment |
| `install`, `hecklr` | `uv tool install hecklr`, `hecklr` | how to install and call Hecklr |
| `github-token` | `github.token` | needs `actions: read` and `pull-requests: write` |

Outputs: `exit-code`, `run-id`, `baseline-run-id`.

## Self-test

`.github/workflows/hecklr-action-selftest.yml` runs this action on the reference agent
(`examples/acme-maintenance`, in process, rules brain, scripted customers, no model calls): a
clean run must exit 0, the seeded fault F3 must exit 1, and a run that cannot start (a model
without a price or an API key) must exit 3 even though a finished run of the same suite is in the store. `selftest/` holds its project file
and agent entry point.
