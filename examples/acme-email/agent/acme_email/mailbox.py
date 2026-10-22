"""The email agent on a real mailbox: new mail is read over IMAP, replies go out over SMTP.

The inbox is polled for ``UNSEEN`` messages; fetching one sets its Seen flag, so a restarted
agent does not answer the same email twice. Each email is answered in its own task: a reply stuck
on a hung tool (F12) never holds up other customers. ``smtplib`` and ``imaplib`` are blocking, so
every mail-server exchange runs in a worker thread, never on the event loop.

The world comes from the email's MIME headers (``X-Hecklr-World-Url``, ``X-Hecklr-Trial-Id``),
which Hecklr's ``smtp-imap`` adapter sets (see ``acme_email.tools``).
"""

import imaplib
import smtplib
from dataclasses import dataclass
from email import message_from_bytes, policy
from email.message import EmailMessage as MimeMessage
from functools import partial
from typing import Any

import anyio
import httpx2
import structlog
from anyio.abc import TaskGroup

from acme_email.desk import EmailDesk
from acme_email.tools import backend_for, lower_case_headers, route_world
from hecklr_adapters.email import from_mime, to_mime
from hecklr_schemas import World

_log = structlog.get_logger(__name__)


@dataclass(frozen=True, slots=True)
class Mailbox:
    """The agent's mailbox and the servers it is reached on."""

    inbox: str
    smtp_host: str = "localhost"
    smtp_port: int = 1025
    imap_host: str = "localhost"
    imap_port: int = 1143
    username: str | None = None
    """IMAP/SMTP login; ``None`` logs into IMAP as ``inbox`` with ``inbox`` as password and skips
    SMTP auth (local test servers such as GreenMail with authentication disabled)."""
    password: str | None = None
    poll_interval_s: float = 1.0
    timeout_s: float = 30.0


def fetch_unseen(mailbox: Mailbox) -> list[bytes]:
    """Every unseen message in the inbox, marked seen as it is fetched (blocking)."""
    user = mailbox.username or mailbox.inbox
    password = mailbox.password or mailbox.inbox
    imap = imaplib.IMAP4(mailbox.imap_host, mailbox.imap_port, timeout=mailbox.timeout_s)
    try:
        _ok(imap.login(user, password))
        _ok(imap.select("INBOX"))
        uids = b" ".join(_ok(imap.uid("SEARCH", "UNSEEN"))).split()
        raws = []
        for uid in uids:
            data = _ok(imap.uid("FETCH", uid.decode(), "(BODY[])"))
            raws += [item[1] for item in data if isinstance(item, tuple)]
        return raws
    finally:
        imap.logout()


def send(mailbox: Mailbox, message: MimeMessage) -> None:
    """Send one message over SMTP (blocking)."""
    with smtplib.SMTP(mailbox.smtp_host, mailbox.smtp_port, timeout=mailbox.timeout_s) as smtp:
        if mailbox.username is not None and mailbox.password is not None:
            smtp.login(mailbox.username, mailbox.password)
        smtp.send_message(message)


def _ok(response: tuple[str, list[Any]]) -> list[Any]:
    """The data of a successful IMAP command (``imaplib`` types it loosely, per command)."""
    status, data = response
    if status != "OK":
        raise imaplib.IMAP4.error(f"IMAP command failed: {status} {data!r}")
    return data


async def answer_raw(
    desk: EmailDesk,
    raw: bytes,
    *,
    mailbox: Mailbox,
    world: World,
    client: httpx2.AsyncClient,
    world_base_url: str | None,
) -> None:
    """Answer one received message; failures are logged, since nobody else would hear of them."""
    try:
        headers = message_from_bytes(raw, _class=MimeMessage, policy=policy.default)
        email = from_mime(raw)
        route = route_world(lower_case_headers(headers.items()), base_url=world_base_url)
        reply = await desk.answer(email, lambda: backend_for(route, client, world))
        await anyio.to_thread.run_sync(send, mailbox, to_mime(reply))
    except Exception:  # noqa: BLE001 -- one bad email must not stop the mailbox; logged loudly
        _log.exception("email_reply_failed", inbox=mailbox.inbox, variant=desk.variant)


async def serve_mailbox(
    desk: EmailDesk,
    world: World,
    mailbox: Mailbox,
    *,
    world_base_url: str | None = None,
    transport: httpx2.AsyncBaseTransport | None = None,
) -> None:
    """Answer the inbox's mail until cancelled; a failed poll is logged and retried."""
    # No client timeout: tool calls run under the mailer's own deadline (DeadlineTools).
    async with (
        httpx2.AsyncClient(timeout=None, transport=transport) as client,
        anyio.create_task_group() as tasks,
    ):
        while True:
            await _poll_once(desk, world, mailbox, client, tasks, world_base_url)
            await anyio.sleep(mailbox.poll_interval_s)


async def _poll_once(
    desk: EmailDesk,
    world: World,
    mailbox: Mailbox,
    client: httpx2.AsyncClient,
    tasks: TaskGroup,
    world_base_url: str | None,
) -> None:
    try:
        raws = await anyio.to_thread.run_sync(fetch_unseen, mailbox)
    except (OSError, imaplib.IMAP4.error):
        _log.exception("mailbox_poll_failed", inbox=mailbox.inbox)
        return
    for raw in raws:
        answer = partial(
            answer_raw, mailbox=mailbox, world=world, client=client, world_base_url=world_base_url
        )
        tasks.start_soon(answer, desk, raw)
