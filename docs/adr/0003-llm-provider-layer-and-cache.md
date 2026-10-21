# 0003. Thin LLM provider layer with the contract in schemas and a content-addressed cache

Date: 2026-10-21 · Status: accepted · Workstream: WS0

## Context

The simulator, judges, miner and fixer all call models; the spec asks for a thin in-house layer over
the Anthropic SDK, the OpenAI SDK and any OpenAI-compatible endpoint, a deterministic `FakeLLM` for
tests, and a response cache so "re-running an unchanged trial costs zero tokens". Graders must
accept a model handle without depending on any SDK, and the orchestrator needs cost estimates to
refuse runs that would exceed the budget.

## Decision

- **Contract in `hecklr-schemas`** (`hecklr_schemas.llm`): `Message`, `LLMRequest`, `LLMUsage`,
  `LLMResponse` and the `LLMProvider` Protocol (`async complete(request) -> LLMResponse`).
  Everything that can change an answer (model, system, messages, temperature, max tokens, seed,
  JSON schema) is a field of the frozen request, so the request is its own cache key.
- **Implementations in `hecklr-llm`**: `AnthropicProvider`, `OpenAIProvider` (also used for
  Ollama, vLLM and any OpenAI-compatible `base_url`), `FakeLLM`, `CachedProvider` and a
  `ProviderRouter` that resolves `provider/model` strings (`anthropic/`, `openai/`, `ollama/`,
  `openai-compatible/`, `fake/`). Every one of them is itself an `LLMProvider`, so they compose:
  `CachedProvider(ProviderRouter(...))`.
- **Structured output** is a JSON Schema on the request (`json_schema`), mapped to each API's
  native mechanism (Anthropic `output_config.format`, OpenAI `response_format: json_schema`).
  `complete_structured()` validates the reply into a Pydantic model. No provider-specific types leak.
- **Sampling**: `temperature=None` sends nothing, because current Anthropic models reject explicit
  sampling parameters; determinism comes from the cache and `seed`, not from temperature 0.
- **Cache**: SQLite (stdlib) under `.hecklr/cache/llm.sqlite3`, keyed by SHA-256 of the canonical
  JSON of the request. WAL mode for concurrent processes; SQLite calls run in a worker thread; a
  per-key lock stops two concurrent identical calls from paying twice. A hit returns the stored
  response with `cached=True` and `cost_usd=0`.
- **FakeLLM** is deterministic by construction: replies come from rules (regex on the last
  message), a pure responder function, or a script; otherwise it derives a reply from the request
  hash, and for JSON-schema requests it builds a minimal schema-valid object. It records every call
  and reports token counts estimated from text length, at zero cost.
- **Pricing**: a small table of USD per million tokens for well-known models; local providers
  (`ollama/`, `fake/`) cost zero; unknown models cost zero with a one-time warning.

## Consequences

- Packages depend only on the Protocol and are tested with `FakeLLM`; no unit test needs a network.
- Provider tests run the real SDKs against a mocked HTTP transport, so request mapping is verified
  without credentials.
- The pricing table drifts as vendors change prices; estimates are for budgeting, and actual spend
  is computed from reported usage with the same table.
