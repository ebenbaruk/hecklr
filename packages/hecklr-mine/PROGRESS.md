# hecklr-mine progress

Owner: WS13. Spec: `docs/SPEC.md` → "Miner (`hecklr-mine`)", US-08, WS13 row. ADR: `docs/adr/0013a-miner-artifacts-and-proposal-queue.md`.

## Done

- **Stage 1, ingest** (`ingest.py`, `steps.py`, `otel.py`, `langfuse.py`, `langsmith.py`):
  - Formats: native JSONL, OTLP JSON GenAI spans, Langfuse traces and observations, LangSmith
    runs, CSV with a YAML mapping.
  - The trace formats share one step assembler. It dedupes customer messages that an agent loop
    re-sends, and attaches tool executions to their reply. Tests: `tests/test_ingest.py` (23).
- **Stage 2, scrub** (`scrub.py`, `presidio.py`):
  - Regex rules, with Luhn and mod-97 checks. One placeholder registry per batch.
  - Optional Presidio. It refuses to start without the spaCy model, and its e-mail recognizer is
    excluded because it fetches the public-suffix list over the network.
  - LLM pass over the already-scrubbed text.
  - Tests: `test_ws13_scrubbing_keeps_the_conversation_coherent`,
    `test_ws13_same_entity_same_placeholder_within_and_across_conversations`, and a real-model
    Presidio test (skipped without `en_core_web_sm`; passed locally with the model installed).
- **Stage 3, detect** (`detect.py`, `judges.py`):
  - Four heuristics.
  - `hecklr_grade.JudgeGrader` runs on production transcripts presented as trials. Ids are
    derived from the conversation, so the cache key is stable. The judge is never shown a world
    (`include_world=False`).
  - Optional LLM root-cause summaries. Tests: `tests/test_detect.py`.
- **Stage 4, cluster** (`embed.py`, `cluster.py`):
  - Hashing and OpenAI embedders behind an `Embedder` protocol.
  - Mean-centered HDBSCAN. Default `min_cluster_size` is 2.5% of the failures (at least 5).
  - LLM or keyword labels. Tests: `tests/test_embed_cluster.py`.
- **Stage 5, synthesize** (`synthesize.py`, `store.py`, `pipeline.py`):
  - Mined scenarios load through `hecklr_core.load_model`.
  - Embedding dedup against the suite and the queue.
  - Append-only proposal queue with `decide`. Tests: `tests/test_synthesize_store.py` and
    `tests/test_pipeline.py`.
- **Synthetic log set** (`synthetic.py`): seeded 1,000 conversations with 5 planted modes, PII in
  many conversations (about 45-48% carry a pattern-detectable entity), and the ground truth kept separate.
  - Offline, `test_ws13_miner_recovers_planted_clusters_on_1000_conversations[0,1]`: detection
    recall 1.0, false-positive rate 0, purity 1.0, every mode recall 1.0. Thresholds are
    0.98 / 0.02 / 0.95 / 0.90.
  - Live, `test_ws13_live_miner_recovers_planted_clusters_with_real_models` (luna and
    text-embedding-3-small): purity 1.0, every mode recall 1.0, $0.022.
- **CLI** `hecklr mine` (`packages/hecklr/src/hecklr/commands/mine.py`): 21 tests in
  `packages/hecklr/tests/test_cli_mine.py`. A real run on the acme-maintenance project
  (1,000 conversations, `--embedder openai --llm-summaries --judge resolved`, $0.18) gave
  5 clusters, purity 1.0, detection recall 1.0 and a false-positive rate of 0.9%. The approved
  gas-leak proposal then ran under `hecklr run` against the reference agent (1/1 passed).
- Coverage: 99% (`pytest packages/hecklr-mine --cov`).

- **Review round 1** (PR #64):
  - Concurrent stages (LLM scrub, judges, LLM summaries) run through `tasks.map_concurrently`,
    which raises the first failure as itself, never as an `ExceptionGroup`, so the CLI's exit
    codes hold (4 budget, 3 model failure). Tests: `test_tasks.py` (3),
    `test_ws13_budget_in_a_concurrent_stage_raises_the_bare_error[judges,llm-scrub,summaries]`,
    `test_mine_stops_at_the_budget_inside_concurrent_stages[3]`,
    `test_mine_reports_model_failures_as_harness_errors[3]`.
  - A malformed LLM summary or LLM-scrub reply on one conversation falls back (heuristic summary,
    pattern scrubbing) and is recorded in `summary.json` (`summary_errors`, `scrub_errors`).
    Tests: `test_ws13_one_unusable_summary_reply_does_not_abort_the_run`,
    `test_ws13_an_unusable_llm_scrub_reply_keeps_pattern_scrubbing`,
    `test_mine_keeps_going_when_summary_and_scrub_replies_are_unusable`.
  - LLM-scrub findings are merged batch-wide and match whole words only
    (`test_ws13_llm_findings_apply_across_the_batch`).
  - Embedding spend is capped without a model (`test_ws13_embedding_spend_is_capped_without_a_model`).
  - The CLI warns that names are kept without `--presidio`/`--llm-scrub`.
  - Half the planted failures in the synthetic set now end with an agent sign-off, so
    detection no longer leans only on the unanswered last message (seed 0: abandonment fires on
    298 of 388 planted failures, sentiment on 388, escalation on 133; recall still 1.0).
  - Coverage 99%; hecklr-mine 106 passed, 1 skipped (spaCy model); `packages/hecklr` 222 passed.

## Next

- WS16 builds the approval queue UI on ADR 0013a (`load_queue`, `decide`).
- WS15 (fixer) consumes `FailureCluster`s from `clusters.jsonl`.

## Decisions

- JSONL files rather than DuckDB for mining runs: the CLI and the UI both write the queue (ADR 0013a).
- Embedding prices live in `hecklr_mine.embed`. `hecklr_llm`'s price table is for chat models,
  and embeddings have no output tokens.
- Production judges get no world section: the world is unknown, and an empty "ground truth" made
  the `resolved` judge fail most real successes (seen in the first live CLI run).
- The default persona for mined scenarios skips adversarial personas.

## Open questions

- `hecklr replay` defaults to the first persona by file name, which can be an adversarial one
  (`personas/adversarial/` sorts first). That code is outside WS13's paths, so it is filed as an issue.
