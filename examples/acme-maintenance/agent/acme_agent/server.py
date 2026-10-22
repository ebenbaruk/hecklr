"""The agent as an HTTP chat service, compatible with Hecklr's ``http`` chat adapter.

- ``POST /chat``: ``{"message", "session_id"}`` in, ``{"reply", ...}`` out (``response_path:
  reply``).
- ``POST /chat/stream``: the same request, answered as server-sent events: unnamed events carry
  text deltas; named events ``tool_call`` / ``tool_result`` (only when Hecklr cannot see the
  tools), ``handoff`` and ``end`` carry ``hecklr_schemas`` payloads (ADR 0006a).
- ``GET /healthz``: brain, variant, the agent's own model spend, the turns that failed and where
  the tools went, for ``hecklr doctor``, the fault matrix and humans.

Which world the tools act on, per request (ADR 0007d), the first of:

1. ``world_url`` in the body (a ``{{world_url}}`` request template placeholder);
2. the ``X-Hecklr-World-Url`` header, which Hecklr's chat adapters send for every trial;
3. ``ACME_WORLD_BASE_URL`` + ``/t/<trial id>`` (trial id from the ``X-Hecklr-Trial-Id`` header);
4. a private in-process copy of the world, for manual runs. Hecklr cannot see those calls.

``/healthz`` counts the requests served from each source, so a run that silently fell back to
the local copy shows up there.
"""

from collections import Counter
from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager
from decimal import Decimal
from typing import Literal

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from acme_agent.agent import AcmeAgent, BackendFactory
from acme_agent.tools import HttpWorldTools, LocalWorldTools, Reply
from hecklr_adapters import WORLD_URL_HEADER
from hecklr_schemas import TRIAL_ID_HEADER, HandoffPayload, ToolCallPayload, World

WorldSource = Literal["body", "header", "base_url", "local"]
WORLD_SOURCES: tuple[WorldSource, ...] = ("body", "header", "base_url", "local")


class ChatRequest(BaseModel):
    """One customer message; extra fields a request template adds are ignored."""

    model_config = ConfigDict(extra="ignore")

    message: str = Field(min_length=1)
    session_id: str = Field(min_length=1)
    trial_id: str | None = None
    world_url: str | None = None
    """Base URL of this conversation's world sandbox (its OpenAPI routes are under it)."""


class ChatResponse(BaseModel):
    """The agent's whole answer to one message."""

    reply: str
    handoff: HandoffPayload | None = None
    ended: bool = False
    tool_calls: list[ToolCallPayload] = Field(default_factory=list)
    """Only when the agent used its own local world, which Hecklr cannot observe."""


class Health(BaseModel):
    """``GET /healthz``: what the agent runs and which worlds its tools have used."""

    status: Literal["ok"] = "ok"
    brain: str
    variant: str
    model: str | None
    """The LLM brain's ``provider/model``; ``None`` for the rules brain."""
    fallback_tools: Literal["sandbox_base_url", "local"]
    """Where tools go when a request names no world: ``ACME_WORLD_BASE_URL`` or the local copy."""
    requests_by_world_source: dict[WorldSource, int]
    """Requests served so far per world source; a Hecklr run should only grow ``header``."""
    spent_usd: Decimal
    """What the agent's own model calls have cost so far; Hecklr's budget does not see it."""
    failed_turns: dict[str, int] = Field(default_factory=dict)
    """Turns that failed (HTTP 500) so far, by exception type. A model outage or an exhausted
    quota (``LLMProviderError``) looks like agent errors to Hecklr; this says which they were."""


def sse_frame(event: str | None, data: str) -> str:
    """One server-sent event; multi-line data becomes several ``data:`` lines."""
    lines = [f"event: {event}"] if event is not None else []
    lines.extend(f"data: {line}" for line in data.split("\n"))
    return "\n".join(lines) + "\n\n"


def sse_events(reply: Reply) -> Iterator[str]:
    """The reply as server-sent events, in stream order."""
    for chunk in reply.chunks():
        if chunk.tool_call is not None:
            yield sse_frame("tool_call", chunk.tool_call.model_dump_json())
        elif chunk.tool_result is not None:
            yield sse_frame("tool_result", chunk.tool_result.model_dump_json())
        elif chunk.handoff is not None:
            yield sse_frame("handoff", chunk.handoff.model_dump_json())
        elif chunk.ended:
            yield sse_frame("end", "")
        else:
            yield sse_frame(None, chunk.text)


def create_app(
    agent: AcmeAgent, world: World, *, world_transport: httpx2.AsyncBaseTransport | None = None
) -> FastAPI:
    """The FastAPI app serving ``agent``; ``world`` backs conversations without a sandbox.

    ``world_transport`` replaces the network between the agent and the world sandbox, for tests.
    """
    clients: list[httpx2.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        timeout = agent.settings.tool_timeout_s
        async with httpx2.AsyncClient(timeout=timeout, transport=world_transport) as client:
            clients.append(client)
            yield
            clients.clear()

    app = FastAPI(title="acme-maintenance agent", lifespan=lifespan)
    base_url = agent.settings.world_base_url
    served: Counter[WorldSource] = Counter()
    failures: Counter[str] = Counter()

    def backend_for(body: ChatRequest, request: Request) -> BackendFactory:
        trial_id = request.headers.get(TRIAL_ID_HEADER) or body.trial_id
        source, world_url = resolve_world(body.world_url, request, trial_id, base_url)
        served[source] += 1
        if world_url is None:
            return lambda: LocalWorldTools(world)
        return lambda: HttpWorldTools(clients[0], world_url, trial_id)

    async def answer(body: ChatRequest, request: Request) -> Reply:
        try:
            return await agent.respond(body.session_id, body.message, backend_for(body, request))
        except Exception as exc:
            # Counted for /healthz, then re-raised: the turn still fails as an HTTP 500.
            failures[type(exc).__name__] += 1
            raise

    @app.post("/chat")
    async def chat(body: ChatRequest, request: Request) -> ChatResponse:
        reply = await answer(body, request)
        reported = [step.call for step in reply.steps] if reply.reported_steps else []
        return ChatResponse(
            reply=reply.text, handoff=reply.handoff, ended=reply.ended, tool_calls=reported
        )

    @app.post("/chat/stream")
    async def chat_stream(body: ChatRequest, request: Request) -> StreamingResponse:
        # The whole turn runs before the stream opens, so a failing brain is an HTTP 500 rather
        # than a stream that breaks half way.
        reply = await answer(body, request)
        return StreamingResponse(sse_events(reply), media_type="text/event-stream")

    @app.get("/healthz")
    async def healthz() -> Health:
        return Health(
            brain=agent.settings.brain,
            variant=agent.files.variant,
            model=agent.settings.model if agent.settings.brain == "llm" else None,
            fallback_tools="sandbox_base_url" if base_url else "local",
            requests_by_world_source={source: served[source] for source in WORLD_SOURCES},
            spent_usd=agent.spent_usd,
            failed_turns=dict(failures),
        )

    return app


def resolve_world(
    body_url: str | None, request: Request, trial_id: str | None, base_url: str | None
) -> tuple[WorldSource, str | None]:
    """Where one request's tools go: its source, and the sandbox URL (``None``: local copy).

    An empty ``body_url`` counts as absent: a ``{{world_url}}`` template renders empty when the
    trial has no sandbox.

    Raises:
        HTTPException: 400 when only ``ACME_WORLD_BASE_URL`` could name the world but the
            request carries no trial id.
    """
    if body_url:
        return "body", body_url
    if header_url := request.headers.get(WORLD_URL_HEADER):
        return "header", header_url
    if base_url is None:
        return "local", None
    if trial_id is None:
        raise HTTPException(
            status_code=400,
            detail=f"ACME_WORLD_BASE_URL is set, so every request needs {TRIAL_ID_HEADER}",
        )
    return "base_url", f"{base_url.rstrip('/')}/t/{trial_id}"
