"""The email agent behind an inbound-parse webhook, the protocol of Hecklr's ``webhook`` adapter.

- ``POST /email``: ``{"trial_id", "reply_url", "message": EmailMessage}`` (plus an optional
  ``world_url``), with the ``X-Hecklr-Trial-Id`` and ``X-Hecklr-World-Url`` headers. Answered
  ``202`` at once, as mail providers expect of a webhook; the reply is worked out in the
  background and POSTed as an ``EmailMessage`` to ``reply_url``.
- ``GET /healthz``: variant, brain, mailer settings, where the tools went, replies in flight.

``reply_url`` is POSTed to as given: this is a local test agent that trusts its caller (Hecklr).
Deployed where anyone can reach it, it would let them make it POST anywhere; a real agent sends
replies through its mail provider instead.

A reply that cannot be worked out (the LLM brain failed) or delivered is logged and dropped: the
customer hears nothing, which is exactly what Hecklr must notice.
"""

from collections import Counter
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from typing import Final, Literal

import anyio
import httpx2
import structlog
from anyio.abc import TaskGroup
from fastapi import FastAPI, Request
from pydantic import BaseModel, ConfigDict

from acme_email.desk import EmailDesk
from acme_email.tools import WORLD_SOURCES, WorldRoute, WorldSource, backend_for, route_world
from hecklr_schemas import EmailMessage, World

_log = structlog.get_logger(__name__)

REPLY_POST_TIMEOUT_S: Final = 10.0


class InboundEmail(BaseModel):
    """One customer email as the webhook receives it; unknown fields are ignored."""

    model_config = ConfigDict(extra="ignore")

    message: EmailMessage
    reply_url: str
    trial_id: str | None = None
    world_url: str | None = None


class Accepted(BaseModel):
    """``POST /email``'s answer."""

    status: Literal["accepted"] = "accepted"


class Health(BaseModel):
    """``GET /healthz``."""

    status: Literal["ok"] = "ok"
    transport: Literal["webhook"] = "webhook"
    variant: str
    brain: str
    model: str | None
    thread_replies: bool
    tool_timeout_s: float | None
    requests_by_world_source: dict[WorldSource, int]
    """Emails served per world source; a Hecklr run should only grow ``header`` (or ``body``)."""
    replies_sent: int
    replies_failed: int
    in_flight: int
    """Emails still being answered: with F12 they pile up here while a tool hangs."""


class _Running:
    """What the app's lifespan opens: set before the first request is served."""

    client: httpx2.AsyncClient
    tasks: TaskGroup


def create_app(
    desk: EmailDesk,
    world: World,
    *,
    world_base_url: str | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> FastAPI:
    """The FastAPI app; ``world`` backs conversations without a sandbox.

    ``transport`` replaces the network for the world sandbox and the reply POSTs, for tests.
    """
    running = _Running()
    counts: Counter[str] = Counter()

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # No client timeout: tool calls run under the mailer's own deadline (DeadlineTools), and
        # reply POSTs set theirs per request.
        async with (
            httpx2.AsyncClient(timeout=None, transport=transport) as client,
            anyio.create_task_group() as tasks,
        ):
            running.client, running.tasks = client, tasks
            yield
            tasks.cancel_scope.cancel()

    app = FastAPI(title="acme-maintenance email agent", lifespan=lifespan)

    async def work(inbound: InboundEmail, route: WorldRoute) -> None:
        client = running.client
        counts["in_flight"] += 1
        try:
            reply = await desk.answer(inbound.message, lambda: backend_for(route, client, world))
            response = await client.post(
                inbound.reply_url,
                content=reply.model_dump_json(),
                headers={"Content-Type": "application/json"},
                timeout=REPLY_POST_TIMEOUT_S,
            )
            response.raise_for_status()
            counts["replies_sent"] += 1
        except Exception:  # noqa: BLE001 -- a webhook has no caller to raise to; logged loudly
            counts["replies_failed"] += 1
            _log.exception("email_reply_failed", to=inbound.message.from_addr, variant=desk.variant)
        finally:
            counts["in_flight"] -= 1

    @app.post("/email", status_code=202)
    async def receive(inbound: InboundEmail, request: Request) -> Accepted:
        route = route_world(
            request.headers,
            body_url=inbound.world_url,
            body_trial_id=inbound.trial_id,
            base_url=world_base_url,
        )
        counts[route.source] += 1
        running.tasks.start_soon(work, inbound, route)
        return Accepted()

    @app.get("/healthz")
    async def healthz() -> Health:
        settings = desk.agent.settings
        return Health(
            variant=desk.variant,
            brain=settings.brain,
            model=settings.model if settings.brain == "llm" else None,
            thread_replies=desk.mailer.thread_replies,
            tool_timeout_s=desk.mailer.tool_timeout_s,
            requests_by_world_source={source: counts[source] for source in WORLD_SOURCES},
            replies_sent=counts["replies_sent"],
            replies_failed=counts["replies_failed"],
            in_flight=counts["in_flight"],
        )

    return app
