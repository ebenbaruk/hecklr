"""A minimal email support agent, reachable over the ``webhook`` and ``smtp-imap`` adapters.

Each customer email gets one threaded reply naming the caller's visit, which the agent looks up
in the trial's world: the webhook body's ``world_url``, or the ``X-Hecklr-World-Url`` header of
the email over SMTP (ADR 0007e). A trial's ``tool_called`` criterion therefore proves the world
URL reached an email agent on that transport.

- ``serve_webhook()``: an inbound-parse endpoint (``POST /inbound``) that POSTs the reply to the
  ``reply_url`` it was given.
- ``serve_mailbox(server, inbox)``: polls ``inbox`` over IMAP and replies over SMTP, like a
  helpdesk reading a shared mailbox (GreenMail in Docker in the tests).

Both run on their own threads, as a real agent runs in its own process.
"""

import imaplib
import json
import smtplib
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from email import message_from_bytes, policy
from email.message import EmailMessage as MimeMessage
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Final

import httpx2

from hecklr_adapters import WORLD_URL_HEADER
from hecklr_adapters.email import from_mime, make_reply, to_mime
from hecklr_adapters.email.testing import MailServer
from hecklr_schemas import EmailMessage

AGENT_ADDR: Final = "support@acme.test"
CUSTOMER_ID: Final = "C-1001"


@dataclass
class EmailAgentLog:
    """What the agent saw, for assertions."""

    world_urls: list[str | None] = field(default_factory=list)
    lookups: list[int] = field(default_factory=list)


def _reply(message: EmailMessage, world_url: str | None, log: EmailAgentLog) -> EmailMessage:
    log.world_urls.append(world_url)
    if world_url is None:
        body = "Sorry, our booking system is offline today."
    else:
        response = httpx2.post(
            f"{world_url}/tools/get_appointment", json={"customer_id": CUSTOMER_ID}, timeout=10
        )
        log.lookups.append(response.status_code)
        slot = response.json()["slot"] if response.status_code == 200 else "unknown"
        body = f"Hello,\n\nYour boiler visit is booked for {slot}.\n\nAcme support"
    return make_reply(message, body=body, from_addr=AGENT_ADDR)


@contextmanager
def serve_webhook() -> Iterator[tuple[str, EmailAgentLog]]:
    """Serve ``POST /inbound`` on an ephemeral 127.0.0.1 port; yields its URL and log."""
    log = EmailAgentLog()

    class Inbound(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            post = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            message = EmailMessage.model_validate(post["message"])
            reply = _reply(message, post.get("world_url"), log)
            httpx2.post(post["reply_url"], json=reply.model_dump(mode="json"), timeout=10)
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Length", "0")
            self.end_headers()

        def log_message(self, format: str, *args: object) -> None:
            """Keep test output quiet."""

    server = ThreadingHTTPServer(("127.0.0.1", 0), Inbound)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}/inbound", log
    finally:
        server.shutdown()
        server.server_close()


@contextmanager
def serve_mailbox(server: MailServer, inbox: str = AGENT_ADDR) -> Iterator[EmailAgentLog]:
    """Answer mail to ``inbox`` on ``server`` from a thread until the block ends."""
    log = EmailAgentLog()
    stop = threading.Event()

    def poll() -> None:
        seen: set[bytes] = set()
        while not stop.wait(0.2):
            for raw in _unseen(server, inbox, seen):
                mime = message_from_bytes(raw, _class=MimeMessage, policy=policy.default)
                world_url = mime.get(WORLD_URL_HEADER)
                reply = _reply(from_mime(raw), str(world_url) if world_url else None, log)
                with smtplib.SMTP(server.host, server.smtp_port, timeout=10) as smtp:
                    smtp.send_message(to_mime(reply))

    thread = threading.Thread(target=poll, daemon=True)
    thread.start()
    try:
        yield log
    finally:
        stop.set()
        thread.join(timeout=10)


def _unseen(server: MailServer, inbox: str, seen: set[bytes]) -> list[bytes]:
    imap = imaplib.IMAP4(server.host, server.imap_port, timeout=10)
    try:
        imap.login(inbox, inbox)
        imap.select("INBOX", readonly=True)
        _, found = imap.uid("SEARCH", "ALL")
        raws: list[bytes] = []
        for uid in b" ".join(found).split():
            if uid not in seen:
                _, data = imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
                raws += [item[1] for item in data if isinstance(item, tuple)]
                seen.add(uid)
        return raws
    finally:
        imap.logout()
