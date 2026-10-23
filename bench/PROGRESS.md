# bench (WS17) progress

Owner: WS17. Spec: `docs/SPEC.md` -> SC10, US-12, WS17 row, "Voice simulation engine > Metrics",
"Agent adapters", `hecklr bench`. Decisions: ADR 0017a. Measured rows: [RESULTS.md](RESULTS.md).

## Done

- **Suite** (`bench.yaml`, `suite/`): frozen acme world, 8 guided chat scenarios, 9 scripted voice
  scenarios (3 latency, 3 barge-in, 3 hesitation), 3 calibration calls; version, seed, models and
  trials pinned; SHA-256 suite digest in every row.
- **`hecklr bench`** (`packages/hecklr/src/hecklr/commands/bench/`): `list`, `run`, `reproduce`,
  `site`. Tests in `packages/hecklr/tests/bench/` (34): the US-12 end to end
  (`test_us12_run_writes_a_row_with_every_metric_and_its_interval`,
  `test_us12_reproduce_checks_every_metric_against_the_published_row`,
  `test_us12_reproduce_fails_when_a_published_interval_is_not_met`) against the real acme agent
  process with scripted models; outage refusal, spend attribution, placeholders, process
  readiness, judge scores, reproduction criterion, the site, the Pipecat agent's meter.
  Coverage of the bench modules 95%.
- **Stacks**: 8 defined; 7 measured (see RESULTS.md). New agents: `stacks/agents/pipecat_acme.py`
  (stock Pipecat 1.13 over SmallWebRTC) and `stacks/agents/livekit_acme.py` (acme cascade behind a
  local livekit-server in Docker).
- **Site**: `site/src` (HTML/CSS/JS), built by `hecklr bench site` into `site/dist`, checked in
  headless Chromium.

## Decisions (beyond ADR 0017a)

- The project's OpenAI credits ran out during the first live run (`acme-chat-luna`: 4 of 16
  conversations hit `429 insufficient_quota`). The runner now refuses such a row instead of
  publishing agent errors; Hecklr's own models moved to qwen3 4B on local Ollama with a 32k
  context (`models/`); the cloud Pipecat stack tests AssemblyAI only (no OpenAI TTS).
- The acme cascade caches synthesized replies on disk; its rows are measured warm (the first,
  cold run had p95 7.7 s, all of it first-time Kokoro synthesis of long replies).

## Next / open

- `acme-chat-luna` is defined but unmeasured until OpenAI credits are restored:
  `uv run --env-file .env hecklr bench run --stack acme-chat-luna --budget-usd 0.30`.
- Re-measure every row on an idle machine before quoting numbers publicly (rows record their load).
- Issue #81: parallel first downloads of a voice model race in `hecklr-voice`.
