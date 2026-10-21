"""Network probes for ``hecklr init`` and ``hecklr doctor``: what is listening, and what is it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

import httpx2

DEFAULT_OLLAMA_URL: Final = "http://localhost:11434"
PROBE_TIMEOUT_S: Final = 3.0

AdapterKind = Literal["openai-compatible", "http"]


@dataclass(frozen=True, slots=True)
class AgentProbe:
    """What answered at an agent URL, and the adapter that fits it."""

    adapter: AdapterKind
    url: str
    """The URL to configure: the ``/v1`` base for OpenAI-compatible agents, else as given."""
    reachable: bool
    detail: str
    models: tuple[str, ...] = ()
    """Models an OpenAI-compatible agent lists; empty otherwise."""


def probe_agent(
    url: str, *, transport: httpx2.BaseTransport | None = None, timeout_s: float = PROBE_TIMEOUT_S
) -> AgentProbe:
    """Detect the agent behind ``url``: an OpenAI-compatible server, or a plain HTTP endpoint.

    An OpenAI-compatible server answers ``GET <base>/models`` with a ``data`` list; ``url`` may be
    the server root, its ``/v1`` base or a ``/v1/chat/completions`` endpoint. Anything else that
    answers HTTP is taken as a plain JSON chat endpoint.
    """
    with httpx2.Client(timeout=timeout_s, transport=transport) as client:
        for base in _openai_bases(url):
            models = _get_json(client, f"{base}/models")
            if isinstance(models, Mapping) and isinstance(models.get("data"), list):
                ids = tuple(
                    str(entry["id"])
                    for entry in models["data"]
                    if isinstance(entry, Mapping) and "id" in entry
                )
                detail = f"OpenAI-compatible at {base}"
                return AgentProbe("openai-compatible", base, True, detail, ids)
        try:
            response = client.get(url)
        except httpx2.HTTPError as exc:
            return AgentProbe("http", url, False, f"nothing answered at {url}: {exc}")
    return AgentProbe("http", url, True, f"HTTP {response.status_code} at {url}")


def _openai_bases(url: str) -> list[str]:
    root = url.rstrip("/")
    for suffix in ("/chat/completions", "/v1"):
        root = root.removesuffix(suffix)
    return [f"{root}/v1", root]


def _get_json(client: httpx2.Client, url: str) -> object:
    try:
        response = client.get(url)
        if response.status_code != 200:
            return None
        return response.json()
    except (httpx2.HTTPError, ValueError):
        # Unreachable or not JSON: simply not this kind of server.
        return None


def ollama_models(
    base_url: str = DEFAULT_OLLAMA_URL,
    *,
    transport: httpx2.BaseTransport | None = None,
    timeout_s: float = PROBE_TIMEOUT_S,
) -> list[str] | None:
    """Models a local Ollama serves, or ``None`` if no Ollama answers at ``base_url``."""
    with httpx2.Client(timeout=timeout_s, transport=transport) as client:
        tags = _get_json(client, f"{base_url.rstrip('/')}/api/tags")
    if not isinstance(tags, Mapping) or not isinstance(tags.get("models"), list):
        return None
    return sorted(
        str(model["name"])
        for model in tags["models"]
        if isinstance(model, Mapping) and "name" in model
    )
