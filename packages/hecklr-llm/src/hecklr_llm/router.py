"""Routes ``provider/model`` strings to provider instances."""

import os
from collections.abc import Mapping
from types import TracebackType
from typing import Final, Self

from hecklr_llm.anthropic_provider import AnthropicProvider
from hecklr_llm.errors import ProviderConfigError, UnknownProviderError
from hecklr_llm.fake import FakeLLM
from hecklr_llm.model_names import split_model
from hecklr_llm.openai_provider import OpenAIProvider
from hecklr_schemas import LLMProvider, LLMRequest, LLMResponse, ProviderSettings

DEFAULT_OLLAMA_BASE_URL: Final = "http://localhost:11434/v1"

KNOWN_PROVIDERS: Final = ("anthropic", "openai", "ollama", "openai-compatible", "fake")

# Default environment variable holding each provider's API key.
_DEFAULT_KEY_ENV: Final = {
    "anthropic": "ANTHROPIC_API_KEY",
    "openai": "OPENAI_API_KEY",
    "openai-compatible": "OPENAI_COMPATIBLE_API_KEY",
}
# Default environment variable holding each provider's base URL.
_DEFAULT_URL_ENV: Final = {
    "ollama": "OLLAMA_BASE_URL",
    "openai-compatible": "OPENAI_COMPATIBLE_BASE_URL",
}
# Local servers ignore the key, but the OpenAI SDK refuses to start without one.
_PLACEHOLDER_KEY: Final = "not-needed"


class ProviderRouter:
    """An ``LLMProvider`` that dispatches each request on its model's provider prefix.

    Providers are created on first use, so a run that only uses Ollama never needs an Anthropic
    key. ``providers`` pins an instance for a prefix (tests use it to inject ``FakeLLM`` or a
    mocked client); ``settings`` mirrors ``models.providers`` in ``hecklr.yaml``.
    """

    def __init__(
        self,
        settings: Mapping[str, ProviderSettings] | None = None,
        *,
        providers: Mapping[str, LLMProvider] | None = None,
        env: Mapping[str, str] | None = None,
    ) -> None:
        self._settings = dict(settings or {})
        self._env = dict(os.environ if env is None else env)
        self._providers: dict[str, LLMProvider] = dict(providers or {})
        self._owned: list[AnthropicProvider | OpenAIProvider] = []

    async def complete(self, request: LLMRequest) -> LLMResponse:
        """Complete ``request`` with the provider its model names."""
        return await self.provider_for(request.model).complete(request)

    async def aclose(self) -> None:
        """Close the SDK clients this router created; injected providers belong to the caller."""
        for provider in self._owned:
            await provider.aclose()
        self._owned.clear()

    async def __aenter__(self) -> Self:
        return self

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        tb: TracebackType | None,
    ) -> None:
        await self.aclose()

    def provider_for(self, model: str) -> LLMProvider:
        """Return (creating if needed) the provider serving ``model``."""
        prefix, _ = split_model(model)
        provider = self._providers.get(prefix)
        if provider is None:
            provider = self._create(prefix)
            self._providers[prefix] = provider
            if isinstance(provider, AnthropicProvider | OpenAIProvider):
                self._owned.append(provider)
        return provider

    def _create(self, prefix: str) -> LLMProvider:
        match prefix:
            case "anthropic":
                return AnthropicProvider(api_key=self._api_key(prefix), base_url=self._url(prefix))
            case "openai":
                return OpenAIProvider(api_key=self._api_key(prefix), base_url=self._url(prefix))
            case "ollama":
                return OpenAIProvider(
                    api_key=_PLACEHOLDER_KEY,
                    base_url=self._url(prefix) or DEFAULT_OLLAMA_BASE_URL,
                    output_limit_param="max_tokens",
                )
            case "openai-compatible":
                base_url = self._url(prefix)
                if base_url is None:
                    raise ProviderConfigError(
                        "openai-compatible models need a base URL: set "
                        "models.providers.openai-compatible.base_url in hecklr.yaml or "
                        "OPENAI_COMPATIBLE_BASE_URL"
                    )
                return OpenAIProvider(
                    api_key=self._api_key(prefix) or _PLACEHOLDER_KEY,
                    base_url=base_url,
                    output_limit_param="max_tokens",
                )
            case "fake":
                return FakeLLM()
            case _:
                raise UnknownProviderError(
                    f"unknown model provider {prefix!r}; known: {', '.join(KNOWN_PROVIDERS)}"
                )

    def _api_key(self, prefix: str) -> str | None:
        settings = self._settings.get(prefix)
        env_name = (settings.api_key_env if settings else None) or _DEFAULT_KEY_ENV.get(prefix)
        return self._env.get(env_name) if env_name else None

    def _url(self, prefix: str) -> str | None:
        settings = self._settings.get(prefix)
        if settings is not None and settings.base_url is not None:
            return str(settings.base_url)
        env_name = _DEFAULT_URL_ENV.get(prefix)
        return (self._env.get(env_name) or None) if env_name else None
