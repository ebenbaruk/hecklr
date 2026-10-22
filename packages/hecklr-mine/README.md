# hecklr-mine

Hecklr's miner turns production logs into failure clusters and proposed regression scenarios
(spec → "Miner", US-08). It is part of [Hecklr](../../README.md), owned by WS13, and installed with
`pip install 'hecklr[mine]'`.

```bash
hecklr mine logs.jsonl                              # native JSONL, offline embeddings
hecklr mine traces.json --format otel --embedder openai --llm-summaries --judge resolved
hecklr mine export.csv --format csv --mapping mapping.yaml --no-model
```

## Stages

| Stage | Module | What it does |
| --- | --- | --- |
| 1. Ingest | `ingest`, `otel`, `langfuse`, `langsmith` | Native JSONL, OTLP JSON GenAI spans (`gen_ai.*`), Langfuse trace/observation exports, LangSmith run exports, CSV via a mapping file. Every format is converted into frozen `Conversation`s |
| 2. Scrub | `scrub`, `presidio` | Regex rules (e-mail, phone, street address, postcode, Luhn-checked card, mod-97 IBAN), optional Presidio (`hecklr-mine[presidio]` plus a spaCy model), optional LLM pass. One registry for the whole batch: same entity, same `[KIND_n]` placeholder |
| 3. Detect | `detect`, `judges` | Escalation, abandonment, repeated questions, negative sentiment trend (lexicon), and optional `hecklr_grade` judges on the transcript |
| 4. Cluster | `embed`, `cluster` | Hashing embedder (free, offline) or OpenAI `text-embedding-3-small`. Vectors are mean-centered and grouped with scikit-learn HDBSCAN. Clusters get LLM labels ("fails when …"), or keyword labels offline |
| 5. Synthesize | `synthesize`, `store` | Each cluster's central conversation becomes a `mined` scenario (via `hecklr_sim.extract_scenario`), deduplicated against the suite and the queue, and appended to the review queue |

## Review queue

Results go to `.hecklr/mine/<mine id>/`, and `proposals.jsonl` there is append-only (ADR 0013a).
`hecklr_mine.load_queue(mine_root)` gives the current state of every proposal.
`hecklr_mine.decide(mine_root, id, ProposalStatus.APPROVED, reviewer=..., scenarios_dir=...)`
records a review and, on approval, writes the scenario into the suite.

## CSV mapping

```yaml
conversation_id: chat_id     # column names; these are the defaults' replacements
role: speaker
text: message
timestamp: sent_at           # optional, ISO 8601
metadata: [csat, plan]       # optional columns copied into Conversation.metadata
role_values: { customer: user, bot: agent }
delimiter: ";"
```

## Testing

`generate_log_set(n, seed)` builds a reproducible synthetic log with five planted failure modes;
`evaluate_recovery` scores a mining run against that ground truth. `tests/test_ws13_recovery.py`
(offline) and `tests/test_live_recovery.py` (`-m live`, real models, about $0.05) hold the
thresholds.
