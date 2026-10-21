# hecklr-llm progress

Owner: WS0. Spec: `docs/SPEC.md` → "Simulator" (providers), "Orchestrator" (response cache,
budget), "Testing strategy" (FakeLLM, live tests). Design: `docs/adr/0003-llm-provider-layer-and-cache.md`.

## Done

- Contract re-exported from `hecklr-schemas`: `LLMRequest`, `LLMResponse`, `LLMUsage`, `Message`,
  `LLMProvider`.
- `FakeLLM`: rules → responder → script → content-hash default; schema-valid minimal JSON for
  structured requests; records calls; zero cost; deterministic (Hypothesis-tested).
- `AnthropicProvider` (Messages API, structured output via `output_config.format`, refusal and
  error mapping), `OpenAIProvider` (Chat Completions for `openai/`, `ollama/`,
  `openai-compatible/`; `response_format: json_schema`; `max_tokens` vs `max_completion_tokens`).
- `ProviderRouter`: lazy per-prefix providers from `hecklr.yaml` settings and the environment;
  `aclose()` / `async with` closes the SDK clients it created.
- `CachedProvider`: SQLite (WAL) content-addressed cache, single-flight per key, failures not
  cached, hits are free and flagged `cached`.
- Pricing table, `cost_usd`, `estimate_request_cost` (upper bound for budget planning).
- Review round:
  - Pricing fails closed: unknown paid models raise `UnknownPriceError` before any request, and
    `models.prices` reaches the providers through `ProviderRouter(prices=...)`.
  - Anthropic schemas pass through `anthropic.transform_schema`.
  - Missing keys raise `ProviderConfigError`.
  - `truncated` is set on cut-off replies, and `complete_structured()` rejects them.
  - Cache lookups and the hit rate are logged.
  - `hecklr-schemas` is pinned to `>=0.1,<0.2`.
- `complete_structured()` → validated Pydantic model.
- Tests: 90 unit tests (no network; SDKs run against `httpx2.MockTransport`), 100% line and branch
  coverage; 2 live tests against local Ollama (`uv run pytest packages/hecklr-llm -m live`), passing
  with `qwen3:4b-instruct`.

## Next

- WS1 wires `CachedProvider(ProviderRouter(config.models.providers, prices=config.models.prices))` into the orchestrator and uses
  `estimate_request_cost` for the pre-run budget check.

## Open questions

- Prices are list prices as of 2026-10; refresh the table when vendors change them.
