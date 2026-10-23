"""The ``smtp-imap`` email adapter: customer emails go out over SMTP, replies come in over IMAP.

The standard library's ``smtplib`` and ``imaplib`` are blocking, so every network exchange runs in
a worker thread (``anyio.to_thread``), never on the event loop.

Mailbox access, for the customer side:

- with ``username_env`` / ``password_env``: one account (typically a catch-all for
  ``from_domain``) is searched for mail addressed to each customer address;
- without: each customer address is its own account, logged into with the address as user and
  password. This is how local test servers with authentication disabled (GreenMail) work.

Replies are found by recipient (``SEARCH TO``), never by threading headers (see ``_routing``).
"""

import imaplib
import os
import smtplib
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager, suppress
from email.message import EmailMessage as MimeMessage
from typing import Any, Final

import anyio

from hecklr_adapters.context import session_headers
from hecklr_adapters.email._routing import TrialAddresses
from hecklr_adapters.email.messages import from_mime, to_mime
from hecklr_adapters.errors import AdapterConfigError, AgentError, AgentTimeoutError
from hecklr_schemas import EmailMessage, SessionContext, SmtpImapAdapterConfig

DEFAULT_POLL_INTERVAL_S: Final = 1.0
"""How often the mailbox is checked; email latency is minutes, so a second is plenty."""


class SmtpImapEmailTarget:
    """Sends over SMTP and polls IMAP for replies; safe to share across trials."""

    def __init__(
        self, config: SmtpImapAdapterConfig, *, poll_interval_s: float = DEFAULT_POLL_INTERVAL_S
    ) -> None:
        """Read credentials now, so a missing variable fails before any trial.

        Raises:
            AdapterConfigError: only one of ``username_env`` / ``password_env`` is set, or a
                named variable is unset.
        """
        self._config = config
        self._poll_interval_s = poll_interval_s
        self._credentials = _credentials(config)
        self._addresses = TrialAddresses()

    async def deliver(self, msg: EmailMessage, ctx: SessionContext) -> None:
        """Send ``msg`` to the agent over SMTP, tagged with the trial and world URL headers."""
        self._addresses.add(ctx.trial_id, msg.from_addr)
        mime = to_mime(msg, session_headers(ctx))
        await anyio.to_thread.run_sync(self._send, mime, ctx.timeout_s)

    async def replies(self, ctx: SessionContext) -> AsyncIterator[EmailMessage]:
        """Poll the customer mailboxes of this trial; never ends by itself.

        Once the mailbox has answered a poll, a failed poll is retried at the next interval:
        one dropped IMAP connection on a busy server is not the agent failing, and failing the
        trial for it made a timeout read as an agent error at random (issue #70). A failure is
        raised when the trial's first poll fails (a wrong or dead server, reported at once) or
        when polls have kept failing for ``ctx.timeout_s``.
        """
        seen: dict[str, set[bytes]] = {}
        answered_at: float | None = None
        """When a poll last succeeded, on the event loop's clock."""
        try:
            while True:
                for address in sorted(self._addresses.of(ctx.trial_id)):
                    known = seen.setdefault(address, set())
                    try:
                        fresh = await anyio.to_thread.run_sync(
                            self._fetch_new, address, known, ctx.timeout_s
                        )
                    except AgentError:
                        if (
                            answered_at is None
                            or anyio.current_time() - answered_at >= ctx.timeout_s
                        ):
                            raise
                        continue
                    answered_at = anyio.current_time()
                    for raw in fresh:
                        yield from_mime(raw)
                await anyio.sleep(self._poll_interval_s)
        finally:
            self._addresses.discard(ctx.trial_id)

    # --- blocking, in worker threads -------------------------------------------------------

    def _send(self, mime: MimeMessage, timeout_s: float) -> None:
        host, port = self._config.smtp_host, self._config.smtp_port
        with (
            _mail_errors(f"SMTP server {host}:{port}", timeout_s),
            smtplib.SMTP(host, port, timeout=timeout_s) as smtp,
        ):
            if self._credentials is not None:
                smtp.login(*self._credentials)
            smtp.send_message(mime)

    def _fetch_new(self, address: str, known: set[bytes], timeout_s: float) -> list[bytes]:
        """Messages to ``address`` not fetched before; adds their UIDs to ``known``."""
        host, port = self._config.imap_host, self._config.imap_port
        user, password = self._credentials or (address, address)
        with (
            _mail_errors(f"IMAP server {host}:{port}", timeout_s),
            _imap(host, port, timeout_s) as imap,
        ):
            _ok(imap.login(user, password))
            _ok(imap.select("INBOX", readonly=True))
            found = _ok(imap.uid("SEARCH", "TO", _quoted(address)))
            uids = [uid for uid in b" ".join(found).split() if uid not in known]
            raws = [_fetch(imap, uid) for uid in uids]
        known.update(uids)
        return raws


def _credentials(config: SmtpImapAdapterConfig) -> tuple[str, str] | None:
    names = (config.username_env, config.password_env)
    if names == (None, None):
        return None
    if None in names:
        raise AdapterConfigError("smtp-imap needs both username_env and password_env, or neither")
    values = [os.environ.get(name) for name in names if name is not None]
    missing = [name for name, value in zip(names, values, strict=True) if not value]
    if missing:
        raise AdapterConfigError(f"smtp-imap: environment variable {missing[0]} is not set")
    return values[0] or "", values[1] or ""


@contextmanager
def _imap(host: str, port: int, timeout_s: float) -> Iterator[imaplib.IMAP4]:
    imap = imaplib.IMAP4(host, port, timeout=timeout_s)
    try:
        yield imap
    finally:
        _logout(imap)


def _logout(imap: imaplib.IMAP4) -> None:
    # Everything was fetched before logging out; a failed logout only means the server drops the
    # connection itself, and raising here would discard messages already read.
    with suppress(OSError, imaplib.IMAP4.error):
        imap.logout()


def _fetch(imap: imaplib.IMAP4, uid: bytes) -> bytes:
    data = _ok(imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])"))
    for item in data:
        if isinstance(item, tuple) and isinstance(item[1], bytes):
            return item[1]
    raise AgentError(f"IMAP FETCH of UID {uid.decode()} returned no message")


def _ok(response: tuple[str, list[Any]]) -> list[Any]:
    """The data of a successful IMAP command (``imaplib`` types it loosely, per command)."""
    status, data = response
    if status != "OK":
        raise AgentError(f"IMAP command failed: {status} {data!r}")
    return data


def _quoted(address: str) -> str:
    return '"' + address.replace("\\", "\\\\").replace('"', '\\"') + '"'


@contextmanager
def _mail_errors(where: str, timeout_s: float) -> Iterator[None]:
    """The agent's mail server failing is the agent's failure, like an unreachable HTTP agent."""
    try:
        yield
    except TimeoutError as exc:
        raise AgentTimeoutError(f"{where} did not answer within {timeout_s} s") from exc
    except (OSError, smtplib.SMTPException, imaplib.IMAP4.error) as exc:
        raise AgentError(f"{where} failed: {type(exc).__name__}: {exc}") from exc
