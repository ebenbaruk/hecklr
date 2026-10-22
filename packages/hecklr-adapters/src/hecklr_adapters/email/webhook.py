"""The ``webhook`` email adapter, in the style of SendGrid / Postmark inbound parse.

Hecklr POSTs each customer email to the agent's ``url`` as JSON::

    {"trial_id": "...", "world_url": "...", "world_mcp_url": "...",
     "reply_url": "http://<listen_host>:<listen_port>/hecklr/email/replies",
     "message": <EmailMessage>}

with the ``X-Hecklr-Trial-Id`` and world URL headers (``session_headers``; the world fields and
headers only when the trial has a sandbox) and any configured ``headers``. The agent answers,
whenever it likes, by POSTing an ``EmailMessage`` as JSON to ``reply_url``. Hecklr routes the
reply by its recipient (see ``_routing``) and answers ``202`` (accepted), ``400`` (not a valid
``EmailMessage``) or ``404`` (no live conversation has that address).

The reply endpoint is a standard-library HTTP server on its own thread: it outlives any one trial
and the ``EmailTarget`` Protocol has no lifecycle hooks, so it starts on first use and stops on
``close()``.
"""

import threading
from collections.abc import AsyncIterator
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

import anyio
import httpx2
from pydantic import ValidationError

from hecklr_adapters._http import agent_http_errors, check_status, new_client
from hecklr_adapters.context import session_fields, session_headers
from hecklr_adapters.email._routing import TrialAddresses
from hecklr_schemas import EmailMessage, SessionContext, WebhookAdapterConfig

REPLY_PATH: Final = "/hecklr/email/replies"
POLL_INTERVAL_S: Final = 0.05
"""How often ``replies`` checks the inbox the server thread fills."""
_MAX_BODY_BYTES: Final = 25 * 1024 * 1024
"""Generous for attachments (the usual mail size cap), small enough to refuse abuse."""


class WebhookEmailTarget:
    """Posts customer emails to a webhook and collects replies on a local endpoint."""

    def __init__(
        self,
        config: WebhookAdapterConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
    ) -> None:
        """``transport`` replaces the network for the outbound POST, for tests."""
        self._config = config
        self._transport = transport
        self._url = str(config.url)
        self._addresses = TrialAddresses()
        self._lock = threading.Lock()
        self._inbox: dict[str, list[EmailMessage]] = {}
        self._server: ThreadingHTTPServer | None = None

    @property
    def reply_url(self) -> str:
        """Where the agent POSTs its replies."""
        return f"http://{self._config.listen_host}:{self._config.listen_port}{REPLY_PATH}"

    def start(self) -> None:
        """Start the reply endpoint if it is not running; idempotent.

        Raises:
            OSError: the port cannot be bound (a harness problem, not the agent's).
        """
        with self._lock:
            if self._server is not None:
                return
            address = (self._config.listen_host, self._config.listen_port)
            server = ThreadingHTTPServer(address, _handler_for(self))
            server.daemon_threads = True
            threading.Thread(
                target=server.serve_forever, name="hecklr-email-webhook", daemon=True
            ).start()
            self._server = server

    def close(self) -> None:
        """Stop the reply endpoint."""
        with self._lock:
            server, self._server = self._server, None
        if server is not None:
            server.shutdown()
            server.server_close()

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        """POST ``msg`` to the agent's webhook."""
        self.start()
        self._addresses.add(ctx.trial_id, msg.from_addr)
        body = {
            **session_fields(ctx),
            "reply_url": self.reply_url,
            "message": msg.model_dump(mode="json"),
        }
        headers = {**self._config.headers, **session_headers(ctx)}
        with agent_http_errors(self._url, self._config.timeout_s):
            async with new_client(self._config.timeout_s, self._transport) as client:
                response = await client.post(self._url, json=body, headers=headers)
                await check_status(response)

    async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        """Stream replies posted for this trial; never ends by itself."""
        self.start()
        try:
            while True:
                with self._lock:
                    arrived = self._inbox.pop(ctx.trial_id, [])
                for reply in arrived:
                    yield reply
                await anyio.sleep(POLL_INTERVAL_S)
        finally:
            self._addresses.discard(ctx.trial_id)
            with self._lock:
                self._inbox.pop(ctx.trial_id, None)

    def accept(self, raw: bytes) -> tuple[HTTPStatus, str]:
        """File a posted reply under its trial; the HTTP status and message for the agent."""
        try:
            reply = EmailMessage.model_validate_json(raw)
        except ValidationError as exc:
            return HTTPStatus.BAD_REQUEST, f"not an EmailMessage: {exc.errors()[0]['msg']}"
        trial_id = next(
            (t for a in reply.to_addrs if (t := self._addresses.trial_of(a)) is not None), None
        )
        if trial_id is None:
            return HTTPStatus.NOT_FOUND, f"no live conversation for {', '.join(reply.to_addrs)}"
        with self._lock:
            self._inbox.setdefault(trial_id, []).append(reply)
        return HTTPStatus.ACCEPTED, "accepted"


def _handler_for(target: WebhookEmailTarget) -> type[BaseHTTPRequestHandler]:
    class _ReplyHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            if self.path != REPLY_PATH:
                self._answer(HTTPStatus.NOT_FOUND, f"replies go to {REPLY_PATH}")
                return
            length = int(self.headers.get("Content-Length") or 0)
            if length > _MAX_BODY_BYTES:
                self._answer(HTTPStatus.REQUEST_ENTITY_TOO_LARGE, "reply too large")
                return
            self._answer(*target.accept(self.rfile.read(length)))

        def _answer(self, status: HTTPStatus, message: str) -> None:
            data = message.encode()
            self.send_response(status)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def log_message(self, format: str, *args: object) -> None:
            """Silence per-request stderr logging; outcomes reach the agent as status codes."""

    return _ReplyHandler
