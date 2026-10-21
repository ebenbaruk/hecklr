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
- WS7d (luna switch, ADR 0007d): `openai/gpt-6-luna` ($0.10/$0.50, cached $0.01) and
  `openai/gpt-5.6-luna` ($0.20/$1.20, cached $0.02) priced; cached prompt tokens billed at the
  cached rate (`CACHED_INPUT_PER_MTOK`, `cost_usd(..., cached_input_tokens=)`). Live probe of both
  models (2026-10-21): `max_tokens` rejected (HTTP 400), `max_completion_tokens` accepted,
  `temperature` other than 1 rejected unless `reasoning_effort: none`, `seed` and JSON Schema
  `response_format` accepted, function tools only with `reasoning_effort: none`, `minimal` not
  supported. The provider sends `reasoning_effort: none` for these models when a request sets a
  temperature. Tests: `test_ws7d_*` in `test_pricing_and_structured.py` and
  `test_providers_http.py` (101 unit tests, 100% coverage); `test_live_openai.py` 3 passed on
  `gpt-6-luna`.

## Next

- WS1 wires `CachedProvider(ProviderRouter(config.models.providers, prices=config.models.prices))` into the orchestrator and uses
  `estimate_request_cost` for the pre-run budget check.

## Open questions

- Prices are list prices as of 2026-10; refresh the table when vendors change them.
