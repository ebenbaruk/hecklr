# Miner: production logs to regression scenarios

The scenarios you can think of before launch are not the ones that break in production. The miner
reads a log export, finds the conversations that went wrong, groups them by root cause, and
proposes a new regression scenario per group for a human to approve.

```bash
pip install "hecklr[mine]"
hecklr mine logs.jsonl                                         # native JSONL, offline embeddings
hecklr mine traces.json --format otel --embedder openai --llm-summaries --judge resolved
hecklr mine export.csv --format csv --mapping mapping.yaml --no-model
```

## The five stages

1. **Ingest.** Native JSONL (one conversation per line, below), OpenTelemetry GenAI spans (OTLP
   JSON), Langfuse and LangSmith exports, or any CSV through a column mapping.
2. **Scrub personal data.** Pattern rules (e-mail, phone, street address, postcode, Luhn-checked
   card numbers, IBANs), optionally Presidio (`--presidio`) and an LLM pass for free text such as
   names (`--llm-scrub`). Scrubbing is consistent across the whole batch: the same entity always
   becomes the same placeholder (`[EMAIL_3]`), so conversations stay coherent.
3. **Detect failures.** Escalation to a human, abandonment, repeated questions, a negative
   sentiment trend, and optionally LLM judges on the transcript (`--judge resolved`).
4. **Cluster.** Embed a summary of each failure (a free offline hashing embedder, or OpenAI
   embeddings), cluster with HDBSCAN, and label each cluster ("fails when the customer gives two
   addresses") with an LLM, or with keywords offline. A blank label from the model falls back to
   the keyword label.
5. **Synthesize.** Turn each cluster's most central conversation into a `mined` scenario,
   deduplicate it against your suite and the queue, and append it to the review queue.

!!! warning "Names are not scrubbed by default"
    The pattern rules find e-mails, phones, addresses and card numbers, not person names. Add
    `--presidio` or `--llm-scrub` for names; the CLI warns when neither is set.

## Spending cap

`--budget-usd` (default: `budget.usd` in `hecklr.yaml`) caps model and embedding spend, with the
same meaning as for `hecklr run`: spend may reach the cap but not cross it. Once spend has reached
it, a call to a priced model is refused (exit code 4) while free models keep going, so
`--budget-usd 0` mines with free models only: `fake/`, `ollama/`, or any model priced at 0 under
`models.prices`. With `--budget-usd 0`, `--embedder openai` is refused before it embeds anything.
The cap is checked before each call, so the calls already in flight (up to 8) can overshoot it
slightly.

## Input format

```json
{"id": "prod-88213", "channel": "chat", "started_at": "2026-10-20T09:14:03Z",
 "turns": [
   {"role": "user", "text": "I need to move Thursday's boiler visit", "t_ms": 0},
   {"role": "agent", "text": "Of course. What's your customer number?", "t_ms": 1840,
    "tool_calls": []}
 ],
 "metadata": {"csat": 2, "escalated": true}}
```

(One conversation per line in the file; shown wrapped here.) CSV exports map their columns with a
small YAML file:

```yaml
conversation_id: chat_id
role: speaker
text: message
timestamp: sent_at             # optional, ISO 8601
metadata: [csat, plan]         # optional columns copied into the conversation's metadata
role_values: { customer: user, bot: agent }
delimiter: ";"
```

## Reviewing proposals

Results go to `.hecklr/mine/<mine id>/`: the scrubbed conversations, the clusters with their
labels and members, a `summary.json`, and `proposals.jsonl`, an append-only review queue. Approve,
edit or reject each proposed scenario in the [local UI](ui.md) (**Approvals**); an approved
scenario is written to `scenarios/mined/`, tagged `mined`. It runs in every suite whose glob,
`tags` and `channels` match it: a project made by `hecklr init` runs the `mined` tag in its `ci`
suite. If no suite would run it (say your suite keeps only `tags: [core]`), the approval says so
and names the filter; add `mined` to the suite's tags, or give mined scenarios a suite of their
own.

## Replay instead of mining

To turn specific conversations into scenarios directly, without clustering:

```bash
hecklr replay transcripts.jsonl --persona polite-regular
```

Each conversation becomes a `replay` scenario: an LLM extracts the customer's goal and the facts
they revealed, and the scenario then runs as a guided simulation. Your agent's new behavior gets a
realistic customer to talk to, instead of a brittle script that breaks as soon as the agent words
something differently.

## How well it works

`hecklr_mine` ships a reproducible synthetic log set with five planted failure modes, and a scorer
that checks whether mining recovers them; the thresholds are tests (offline, and with real models
under `-m live`).
