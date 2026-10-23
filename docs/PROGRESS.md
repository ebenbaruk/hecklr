# WS18 docs and launch: progress

Owner: WS18. Spec: `docs/SPEC.md` → SC1, SC10, "Developer experience", "Testing strategy"
(executed docs), "Milestones, demo and launch". Owned paths: `README.md`, `docs/**` (not the spec,
other workstreams' ADRs or milestone reports), `mkdocs.yml`, `CONTRIBUTING.md`,
`CODE_OF_CONDUCT.md`, `SECURITY.md`, `.github/ISSUE_TEMPLATE/**`, `.github/workflows/docs.yml`,
`tests/docs/**`, `demo/**`. This file is excluded from the site.

## Plan

1. Docs site (MkDocs Material, `mkdocs.yml`): quickstart, concepts, statistics explainer, adapters,
   guides (voice, CI, red team, miner, fixer, UI), generated CLI and configuration reference,
   comparison, FAQ, roadmap. `mkdocs build --strict`, built locally and in CI, never deployed.
2. Executed docs (`tests/docs/`): SC1 quickstart run literally; every documented `hecklr` command
   parsed by the CLI; every titled project-file YAML block validated against its schema; the
   generated reference pages compared with the code.
3. README: one-screen pitch, real demo GIF, 3-command quickstart (the same block SC1 runs),
   features, comparison table, mermaid architecture, CI badges.
4. Demo (`demo/`): real runs of the 90-second demo flow, the GIF (VHS), screenshots, `VIDEO.md`
   shot list and script for the human-narrated video.
5. Community files: CONTRIBUTING, Contributor Covenant 2.1, SECURITY, issue forms, >= 10 issues
   labeled `good first issue`.
6. `docs/milestones/LAUNCH.md`: launch checklist with status, drafts (not posted) of the blog post,
   Show HN, X thread and LinkedIn post, from real numbers.

## Decisions

- **Reference pages are generated and committed** (`docs/scripts/gen_reference.py`): the CLI page
  is Typer's own `typer ... utils docs` output; the configuration page walks the pydantic models
  `hecklr` loads (`ProjectConfig`, `Scenario`, `Persona`, `World`, success criteria) and their field
  docstrings. Committing them keeps the site buildable without importing Hecklr; a test fails when
  they drift.
- **Repository documents stay out of the site** (`exclude_docs`: SPEC, ADRs, RFCs, milestones):
  they link into `packages/` and `examples/`, which strict mode rejects. The site links to them on
  GitHub.
- **`mkdocs<2`** in the `docs` dependency group: MkDocs 2 removes the plugin and theme system
  Material for MkDocs is built on.
- **SC1 as an offline laptop.** The quickstart test strips model keys and makes `localhost:11434`
  unreachable through proxy variables so `hecklr init` picks scripted `fake/` models on a machine
  that runs Ollama too (`hecklr init` probes a fixed URL; #74 asks it to honor `OLLAMA_BASE_URL`).
- **SC1 on both agents** (review round 1). SC1 says "quickstart on reference agent", so the test
  checks the three-command quickstart on the starter agent `hecklr init --yes` writes, and then
  the quickstart's "Point it at your agent" block (`hecklr init --yes --agent-url ... --force`,
  `hecklr run`) against `acme-maintenance` started with the documented `serve.py --brain rules`
  command; agent start to first report under 300 s. The documented `localhost:8000` becomes
  `127.0.0.1:<free port>` (the proxy variables that hide Ollama spare only `127.0.0.1`) and
  `uv run python` becomes the test interpreter: the only changes to the documented commands.
- **Demo on the rules brain.** The chat demo's numbers come from the reference agent's
  deterministic rules brain on the scripted `smoke` suite: free and exactly repeatable. The spec's
  example numbers ("down 7 points") are replaced by the measured ones.

## Done (2026-10-23)

- Site: 17 pages plus 2 generated references; `uv run --group docs mkdocs build --strict` clean.
- Executed docs, `tests/docs` (12 tests): `test_sc1_quickstart_init_to_first_report_under_5_minutes`
  (about 10 s on an idle laptop), `test_sc1_quickstart_on_reference_agent_under_5_minutes` (about
  16 s idle, agent start included), `test_documented_hecklr_commands_parse`,
  `test_documented_project_files_validate[...]` (7 blocks), `test_docs_show_project_files_for_every_kind`,
  `test_generated_reference_pages_match_the_code`. CI: `.github/workflows/docs.yml`.
- Demo from real runs, numbers in `demo/RESULTS.md`: chat gate REGRESSION −27.3 pts
  [−42.4, −12.1], p = 0.004, 9 flips ($0); voice clean vs F8 barge-in p95 212 → 2,731 ms,
  −87.5 pts [−100, −62.5], p = 0.016 ($0); judged run $0.0183; loopback calibration worst 9 ms.
  GIF (VHS, 363 KB) and 4 screenshots in `docs/assets/` (1.4 MB total).
- Community: CONTRIBUTING, Contributor Covenant 2.1, SECURITY, issue forms; 11 issues labeled
  `good first issue` with guidance comments; filed #74 (init and `OLLAMA_BASE_URL`) and #76 (cost
  estimate below actual).
- `docs/milestones/LAUNCH.md`: checklist with status and the four drafts (not posted).

## Next

- Done at integration: `docs/guides/fixer.md`, README, index and roadmap describe the merged
  `hecklr fix` and `hecklr bench`; the CLI reference is regenerated; `scripts/mypy-all.sh` covers
  `tests/docs`, `docs/scripts` and `demo/` (`demo/screenshots.py` with Playwright added for that
  one call).
- The demo's fifth beat (`hecklr fix`) needs a real patch-writing model (OpenAI credit exhausted).
- `.pre-commit-config.yaml` is outside WS18's paths; this PR's change to it is disclosed in the PR
  for the integrator to acknowledge.
- Launch-day items in LAUNCH.md (public repo, domain, PyPI, hosting the site, the video).

## Open questions

- Where the site will be hosted (GitHub Pages under the `hecklr` org is the obvious choice) and
  whether `hecklr.dev` is available (spec open question).
