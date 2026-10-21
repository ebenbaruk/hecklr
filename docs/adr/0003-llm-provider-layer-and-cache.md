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
  Anthropic accepts only a subset of JSON Schema (`additionalProperties: false` on every object,
  no numeric or length constraints), so the schema passes through the SDK's `transform_schema`
  first, which moves unsupported keywords into descriptions. `complete_structured()` validates the
  reply against the full Pydantic model. A reply cut off at `max_tokens` comes back with
  `truncated=True`, and `complete_structured()` raises on it. No provider-specific types leak.
- **Sampling**: the OpenAI-compatible provider forwards `temperature` and `seed` when set. The
  Anthropic provider never sends them. The API has no seed, and Opus 4.7+ and the 5.x models reject
  sampling parameters; Sonnet 4.6 and Haiku 4.5 still accept a temperature, but one code path is
  simpler. On Anthropic, temperature and seed therefore only shape the cache key. Reproducibility
  comes from the cache, not from temperature 0.
- **Cache**: SQLite (stdlib) under `.hecklr/cache/llm.sqlite3`, keyed by SHA-256 of the canonical
  JSON of the request. SQLite calls run in a worker thread. A per-key lock stops two concurrent
  identical calls in one process from paying twice. WAL mode and `INSERT OR IGNORE` make the file
  safe for several processes, but two processes racing on the same new request may both pay (the
  first stored row wins). A hit returns the stored response with `cached=True` and `cost_usd=0`.
  The key names the model string, not the endpoint: two servers both serving `ollama/<model>`
  share entries, so give different endpoints different cache files. Each lookup and the hit rate
  on close are logged (`llm.cache`, `llm.cache_summary`).
- **FakeLLM** is deterministic by construction: replies come from rules (regex on the last
  message), a pure responder function, or a script; otherwise it derives a reply from the request
  hash, and for JSON-schema requests it builds a minimal schema-valid object. It records every call
  and reports token counts estimated from text length, at zero cost.
- **Pricing fails closed**: a small table of USD per million tokens for well-known models, extended
  or overridden by `models.prices` in `hecklr.yaml`. Local providers (`ollama/`, `fake/`) cost zero.
  Any other model without a price raises `UnknownPriceError`, both when the budget is estimated
  and before a request is sent. A model that counted as free would bypass the budget cap. A
  self-hosted `openai-compatible` model is declared at price 0.
- **Keys**: the router reads API keys from its `env` mapping and always passes them explicitly. A
  missing key is a `ProviderConfigError` that names the variable, never a raw SDK error, and the
  SDKs never fall back to the process environment.

## Consequences

- Packages depend only on the Protocol and are tested with `FakeLLM`; no unit test needs a network.
- Provider tests run the real SDKs against a mocked HTTP transport, so request mapping is verified
  without credentials.
- The pricing table drifts as vendors change prices; estimates are for budgeting, and actual spend
  is computed from reported usage with the same table.
