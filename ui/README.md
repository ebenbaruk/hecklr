# Hecklr UI (`hecklr ui`)

The local web UI over a project's stored runs: React + Vite + TypeScript + Tailwind, served by
the FastAPI backend in `packages/hecklr/src/hecklr/ui_api/` (spec, "Local UI"; ADR 0016a).

| View | Route | What it shows |
| --- | --- | --- |
| Runs | `/` | Every run with status, pass rate and cost per trial (each with its 95% CI); filters; pick two to compare |
| Run | `/runs/:id` | Metrics with intervals (slices on demand), scenarios with Wilson intervals, trials |
| Compare | `/compare?baseline=&candidate=` | Suite verdict, Δ with CI per metric and slice (p, BH q), flips grouped by cause |
| Trial | `/runs/:id/trials/:trial` | Transcript, tool-call timeline, world diff, grades with judge rationale and evidence links, waveform with turn markers (wavesurfer.js) |
| Clusters | `/clusters` | The miner's failure clusters, members linked to trials or shown as conversations |
| Approvals | `/proposals` | Proposed scenarios: edit the YAML, approve (written to `scenarios/mined/`) or reject |
| Labeling | `/labels` | Blind pass/fail labels on judge grades (stratified by rubric), κ/precision/recall with CIs |

Everything is read-only on results; labels go to `.hecklr/labels.jsonl` (what
`hecklr calibrate --judges --labels` reads) and decisions are appended to the miner's
`proposals.jsonl`.

## Use

```bash
hecklr ui                      # http://127.0.0.1:8484/, opens a browser
hecklr ui --port 9000 --user ada --no-open
```

The built app ships inside the `hecklr` wheel; `pip install "hecklr[ui]"` needs no Node.

## Develop

```bash
npm ci
uv run hecklr ui --dev -c path/to/hecklr.yaml   # the API on :8484
npm run dev                                      # Vite on :5173, proxies /api (HECKLR_API overrides)
npm run lint && npm run typecheck && npm test    # ESLint, tsc, Vitest + coverage (>= 85% lines)
npm run build                                    # into packages/hecklr/src/hecklr/ui_api/static
```

Tests use Testing Library with `fetch` faked per path (`src/test/helpers.tsx`); the comparison and
chat-trial fixtures in `src/test/fixtures/` are real API responses captured from the
acme-maintenance smoke suite (clean agent vs fault F1).
