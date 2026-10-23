"""Network probes for ``hecklr init`` and ``hecklr doctor``: what is listening, and what is it."""

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final, Literal

import httpx2

DEFAULT_OLLAMA_URL: Final = "http://localhost:11434"
OLLAMA_URL_ENV: Final = "OLLAMA_BASE_URL"
"""Where the provider layer reaches Ollama: its OpenAI-compatible ``/v1`` base."""
PROBE_TIMEOUT_S: Final = 3.0

AdapterKind = Literal["openai-compatible", "http"]

_REPLY_FIELDS: Final = ("reply", "response", "answer", "output", "text", "message", "content")
"""Field names agents commonly answer in, most specific first."""


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


def reply_path(
    url: str, *, transport: httpx2.BaseTransport | None = None, timeout_s: float = PROBE_TIMEOUT_S
) -> str | None:
    """The JSON field an HTTP chat agent puts its reply in, found by sending it one message.

    Uses the ``http`` adapter's default request body. Returns ``None`` when the agent does not
    answer with a JSON object holding a text field (the adapter then reads the whole body).
    """
    body = {"message": "Hello", "session_id": "hecklr-init-probe"}
    with httpx2.Client(timeout=timeout_s, transport=transport) as client:
        try:
            response = client.post(url, json=body)
            reply = response.json() if response.status_code == 200 else None
        except (httpx2.HTTPError, ValueError):
            # Unreachable or not JSON: keep the adapter's default of reading the whole body.
            return None
    if not isinstance(reply, Mapping):
        return None
    texts = [key for key, value in reply.items() if isinstance(value, str)]
    preferred = [key for key in _REPLY_FIELDS if key in texts]
    return (preferred or texts or [None])[0]


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


def local_ollama_url(env: Mapping[str, str]) -> str:
    """The Ollama server to probe: ``OLLAMA_BASE_URL`` without its ``/v1``, else the default."""
    configured = env.get(OLLAMA_URL_ENV, "").strip() or DEFAULT_OLLAMA_URL
    return configured.rstrip("/").removesuffix("/v1")


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
