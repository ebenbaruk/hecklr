"""HTTP plumbing shared by the ``http`` and ``openai-compatible`` adapters."""

import json
from collections.abc import Iterator
from contextlib import contextmanager

import httpx2
from pydantic import JsonValue

from hecklr_adapters.errors import AgentError, AgentTimeoutError

_ERROR_BODY_CHARS = 200
"""How much of an error response body to quote: enough to diagnose, short enough to log."""


def new_client(timeout_s: float, transport: httpx2.AsyncBaseTransport | None) -> httpx2.AsyncClient:
    """One client per session: its connections live and die with the conversation."""
    return httpx2.AsyncClient(timeout=httpx2.Timeout(timeout_s), transport=transport)


@contextmanager
def agent_http_errors(url: str, timeout_s: float) -> Iterator[None]:
    """Report transport failures as the agent's: it is unreachable, stalled or broke the stream."""
    try:
        yield
    except httpx2.TimeoutException as exc:
        raise AgentTimeoutError(f"agent at {url} sent nothing for {timeout_s} s") from exc
    except httpx2.HTTPError as exc:
        raise AgentError(f"agent at {url} failed: {type(exc).__name__}: {exc}") from exc


async def check_status(response: httpx2.Response) -> None:
    """Raise ``AgentError`` for an HTTP error status, quoting the start of the body."""
    if response.status_code < 400:
        return
    await response.aread()
    raise AgentError(
        f"agent returned HTTP {response.status_code}: {response.text[:_ERROR_BODY_CHARS]}"
    )


def parse_json(text: str) -> JsonValue:
    """Decode an agent's JSON; malformed JSON is the agent's fault."""
    try:
        value: JsonValue = json.loads(text)
    except json.JSONDecodeError as exc:
        raise AgentError(f"agent sent invalid JSON ({exc}): {text[:_ERROR_BODY_CHARS]}") from exc
    return value
