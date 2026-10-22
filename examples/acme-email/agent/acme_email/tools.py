"""Which world an email's tools act on, and the deadline every tool call runs under.

The world, per email (ADR 0007d, extended to mail), the first of:

1. ``world_url`` in the webhook body;
2. the ``X-Hecklr-World-Url`` header: an HTTP header on a webhook POST, a MIME header on mail;
3. ``ACME_WORLD_BASE_URL`` + ``/t/<trial id>``, the trial id from ``X-Hecklr-Trial-Id`` (HTTP or
   MIME header) or the webhook body;
4. a private in-process copy of the world, for manual runs. Hecklr cannot see those calls.
"""

import math
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Final, Literal

import anyio
import httpx2
from acme_agent.tools import HttpWorldTools, LocalWorldTools, ToolBackend, ToolOutcome
from pydantic import JsonValue

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_schemas import TRIAL_ID_HEADER, World

WorldSource = Literal["body", "header", "base_url", "local"]
WORLD_SOURCES: tuple[WorldSource, ...] = ("body", "header", "base_url", "local")


@dataclass(frozen=True, slots=True)
class WorldRoute:
    """Where one email's tools go."""

    source: WorldSource
    url: str | None
    """The trial's sandbox base URL; ``None`` for the local copy."""
    trial_id: str | None


def route_world(
    headers: Mapping[str, str],
    *,
    body_url: str | None = None,
    body_trial_id: str | None = None,
    base_url: str | None = None,
) -> WorldRoute:
    """Resolve the world of one email from its webhook body and its (HTTP or MIME) headers.

    Header names are looked up in lower case: pass a case-insensitive mapping (Starlette's
    ``Headers``) or a dict with lower-case keys (``lower_case_headers``). An empty
    ``body_url`` counts as absent (a ``{{world_url}}`` template renders empty without a sandbox).
    """
    trial_id = headers.get(TRIAL_ID_HEADER.lower()) or body_trial_id
    if body_url:
        return WorldRoute("body", body_url, trial_id)
    if header_url := headers.get(WORLD_URL_HEADER.lower()):
        return WorldRoute("header", header_url, trial_id)
    if base_url is not None and trial_id is not None:
        return WorldRoute("base_url", f"{base_url.rstrip('/')}/t/{trial_id}", trial_id)
    return WorldRoute("local", None, trial_id)


def lower_case_headers(items: Iterable[tuple[str, object]]) -> dict[str, str]:
    """Header ``(name, value)`` pairs as a dict ``route_world`` can read; the first one wins."""
    headers: dict[str, str] = {}
    for name, value in items:
        headers.setdefault(name.lower(), str(value))
    return headers


def backend_for(route: WorldRoute, client: httpx2.AsyncClient, world: World) -> ToolBackend:
    """The tool backend ``route`` names: the trial's sandbox over HTTP, or a local copy."""
    if route.url is None:
        return LocalWorldTools(world)
    return HttpWorldTools(client, route.url, route.trial_id)


RETRY_AFTER_S: Final = 30.0
"""How long a tool that missed its deadline counts as down before it is called again."""


class DeadlineTools:
    """Runs another backend's calls under a deadline; a call that misses it is a timeout error.

    A tool that timed out is down for ``retry_after_s``: calls in that window fail at once with
    the same error instead of waiting out the deadline again. The LLM brain retries a failed tool
    several times per reply, and waiting 5 s each time made the clean agent miss the customer's
    reply deadline (PR #30); a real support desk's circuit breaker does the same.

    With no deadline (``timeout_s=None``, fault F12) a hung tool call never returns, so neither
    does the brain's reply.
    """

    def __init__(
        self, backend: ToolBackend, timeout_s: float | None, retry_after_s: float = RETRY_AFTER_S
    ) -> None:
        self._backend = backend
        self._timeout_s = timeout_s
        self._retry_after_s = retry_after_s
        self._down_until: dict[str, float] = {}

    @property
    def reports_calls(self) -> bool:
        """The wrapped backend's answer."""
        return self._backend.reports_calls

    async def call(self, name: str, args: Mapping[str, JsonValue]) -> ToolOutcome:
        """Run one call, giving up at the deadline, or at once while the tool is down."""
        if self._timeout_s is None:
            return await self._backend.call(name, args)
        now = anyio.current_time()
        if now < self._down_until.get(name, -math.inf):
            return _timeout(f"{name} is down: it did not answer within {self._timeout_s:g} s")
        with anyio.move_on_after(self._timeout_s):
            return await self._backend.call(name, args)
        self._down_until[name] = anyio.current_time() + self._retry_after_s
        return _timeout(f"{name} did not answer within {self._timeout_s:g} s")


def _timeout(message: str) -> ToolOutcome:
    return ToolOutcome(ok=False, body={"error": {"code": "timeout", "message": message}})
