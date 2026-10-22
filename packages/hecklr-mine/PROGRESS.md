# hecklr-mine progress

Owner: WS13. Spec: `docs/SPEC.md` → "Miner (`hecklr-mine`)", US-08, WS13 row. ADR: `docs/adr/0013a-miner-artifacts-and-proposal-queue.md`.

## Plan

1. Ingest (`ingest.py`, `otel.py`, `langfuse.py`, `langsmith.py`, `steps.py`): native JSONL, OTLP JSON
   GenAI spans, Langfuse and LangSmith exports, CSV with a mapping file → frozen `Conversation`s.
2. Scrub (`scrub.py`, `presidio.py`): regex rules (email, phone, street address, postcode, Luhn card,
   mod-97 IBAN), optional Presidio (`hecklr-mine[presidio]`), optional LLM pass; one placeholder
   registry for the whole batch, so the same entity always gets the same placeholder.
3. Detect (`detect.py`, `judges.py`): escalation, abandonment, repeated questions, negative
   sentiment trend (lexicon), optional `hecklr_grade` judges on the transcript.
4. Cluster (`embed.py`, `cluster.py`): `Embedder` protocol (offline hashing embedder, OpenAI
   `text-embedding-3-small`), scikit-learn HDBSCAN, LLM labels (fallback: distinctive terms).
5. Synthesize (`synthesize.py`, `store.py`): exemplar → scenario via `hecklr_sim.extract_scenario`,
   re-tagged as `mined`; dedup against the suite and the queue by embedding similarity; proposals
   appended to `<output_dir>/mine/<mine_id>/proposals.jsonl` (newest record per id wins).
6. `synthetic.py`: seeded 1,000-conversation log set with 5 planted failure modes; the miner must
   recover them (purity and recall thresholds in `tests/test_ws13_recovery.py`).
7. CLI `hecklr mine LOGS --format ... --out ...` in `packages/hecklr/src/hecklr/commands/mine.py`.

## Done

- Empty package scaffold (Wave 0): src layout, `py.typed`, smoke test.

## Next

- Everything in the plan above.

## Open questions

- None yet.
