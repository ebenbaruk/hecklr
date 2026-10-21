"""The agent as an HTTP chat service, compatible with Hecklr's ``http`` chat adapter.

- ``POST /chat``: ``{"message", "session_id"}`` in, ``{"reply", ...}`` out (``response_path:
  reply``).
- ``POST /chat/stream``: the same request, answered as server-sent events: unnamed events carry
  text deltas; named events ``tool_call`` / ``tool_result`` (only when Hecklr cannot see the
  tools), ``handoff`` and ``end`` carry ``hecklr_schemas`` payloads (ADR 0006a).
- ``GET /healthz``: brain and variant, for ``hecklr doctor`` and humans.

Which world the tools act on, per conversation: the request's ``world_url``, else the sandbox at
``ACME_WORLD_BASE_URL`` + ``/t/<trial id>`` (trial id from the ``X-Hecklr-Trial-Id`` header),
else a private in-process copy of the world.
"""

from collections.abc import AsyncIterator, Iterator
from contextlib import asynccontextmanager

import httpx2
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, ConfigDict, Field

from acme_agent.agent import AcmeAgent, BackendFactory
from acme_agent.tools import HttpWorldTools, LocalWorldTools, Reply
from hecklr_schemas import TRIAL_ID_HEADER, HandoffPayload, ToolCallPayload, World


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


def create_app(agent: AcmeAgent, world: World) -> FastAPI:
    """The FastAPI app serving ``agent``; ``world`` backs conversations without a sandbox."""
    clients: list[httpx2.AsyncClient] = []

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        async with httpx2.AsyncClient(timeout=agent.settings.tool_timeout_s) as client:
            clients.append(client)
            yield
            clients.clear()

    app = FastAPI(title="acme-maintenance agent", lifespan=lifespan)
    base_url = agent.settings.world_base_url

    def backend_for(body: ChatRequest, request: Request) -> BackendFactory:
        trial_id = request.headers.get(TRIAL_ID_HEADER) or body.trial_id
        world_url = body.world_url
        if world_url is None and base_url is not None:
            if trial_id is None:
                raise HTTPException(
                    status_code=400,
                    detail=f"ACME_WORLD_BASE_URL is set, so every request needs {TRIAL_ID_HEADER}",
                )
            world_url = f"{base_url.rstrip('/')}/t/{trial_id}"
        if world_url is None:
            return lambda: LocalWorldTools(world)
        sandbox_url = world_url
        return lambda: HttpWorldTools(clients[0], sandbox_url, trial_id)

    async def answer(body: ChatRequest, request: Request) -> Reply:
        return await agent.respond(body.session_id, body.message, backend_for(body, request))

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
    async def healthz() -> dict[str, str]:
        return {
            "status": "ok",
            "brain": agent.settings.brain,
            "variant": agent.files.variant,
            "tools": "sandbox" if base_url else "local",
        }

    return app
