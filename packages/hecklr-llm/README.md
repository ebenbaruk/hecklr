# hecklr-llm

The LLM provider layer for [Hecklr](../../README.md): Anthropic, OpenAI and any OpenAI-compatible
endpoint (Ollama, vLLM), a deterministic `FakeLLM` for tests, and an on-disk response cache so an
unchanged call is never paid for twice. Owned by WS0; see `docs/adr/0003-llm-provider-layer-and-cache.md`.

```python
from hecklr_llm import CachedProvider, LLMRequest, Message, ProviderRouter, complete_structured

async with ProviderRouter() as router:
    llm = CachedProvider(router)
    reply = await llm.complete(
        LLMRequest(model="ollama/qwen3:8b", messages=[Message(role="user", content="Hi")])
    )
```

Model strings are `provider/model`: `anthropic/…`, `openai/…`, `ollama/…`,
`openai-compatible/…` (base URL from `hecklr.yaml` or `OPENAI_COMPATIBLE_BASE_URL`), `fake/…`.
