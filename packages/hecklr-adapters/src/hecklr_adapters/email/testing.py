"""Test helpers for email agents: a throwaway GreenMail server in Docker and a tiny mailbox agent.

GreenMail serves SMTP and IMAP with authentication disabled: every address is a mailbox, created
on first use, logged into with any password. That is what ``SmtpImapEmailTarget`` expects when no
credentials are configured. Example::

    with greenmail() as server:
        target = SmtpImapEmailTarget(server.config(inbox="support@acme.test"))
        agent = run_mailbox_agent(server, "support@acme.test", answer)  # run it in a task group

``tests/fixtures/email/compose.yaml`` starts the same server on the default ports for manual runs.
"""

import imaplib
import shutil
import smtplib
import socket
import subprocess
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Final, NoReturn

import anyio

from hecklr_adapters.email.messages import from_mime, to_mime
from hecklr_schemas import EmailMessage, SmtpImapAdapterConfig

GREENMAIL_IMAGE: Final = "greenmail/standalone:2.1.5"
_GREENMAIL_OPTS: Final = (
    "-Dgreenmail.setup.test.smtp -Dgreenmail.setup.test.imap "
    "-Dgreenmail.hostname=0.0.0.0 -Dgreenmail.auth.disabled"
)
_SMTP_PORT, _IMAP_PORT = 3025, 3143
_STARTUP_TIMEOUT_S: Final = 60.0


@dataclass(frozen=True, slots=True)
class MailServer:
    """Where a running test mail server listens."""

    host: str
    smtp_port: int
    imap_port: int

    def config(self, inbox: str, **overrides: object) -> SmtpImapAdapterConfig:
        """An ``smtp-imap`` adapter config for an agent reading ``inbox`` on this server."""
        fields = {
            "type": "smtp-imap",
            "inbox": inbox,
            "smtp_host": self.host,
            "smtp_port": self.smtp_port,
            "imap_host": self.host,
            "imap_port": self.imap_port,
        }
        return SmtpImapAdapterConfig.model_validate(fields | overrides)


def docker_available() -> bool:
    """Whether a Docker daemon answers, so Docker-backed tests can skip cleanly otherwise."""
    docker = shutil.which("docker")
    if docker is None:
        return False
    probe = subprocess.run(  # noqa: S603 -- fixed arguments, no shell
        [docker, "info", "--format", "{{.ServerVersion}}"],
        capture_output=True,
        timeout=20,
        check=False,
    )
    return probe.returncode == 0


@contextmanager
def greenmail(image: str = GREENMAIL_IMAGE) -> Iterator[MailServer]:
    """Run GreenMail on ephemeral 127.0.0.1 ports for the duration of the block.

    Raises:
        RuntimeError: Docker is missing or the server did not come up.
    """
    docker = shutil.which("docker")
    if docker is None:
        raise RuntimeError("docker is not installed")
    started = _docker(
        docker,
        "run", "-d", "--rm",
        "-e", f"GREENMAIL_OPTS={_GREENMAIL_OPTS}",
        "-p", f"127.0.0.1::{_SMTP_PORT}",
        "-p", f"127.0.0.1::{_IMAP_PORT}",
        image,
    )  # fmt: skip
    container = started.strip()
    try:
        server = MailServer(
            "127.0.0.1",
            smtp_port=_host_port(docker, container, _SMTP_PORT),
            imap_port=_host_port(docker, container, _IMAP_PORT),
        )
        _wait_until_ready(server)
        yield server
    finally:
        subprocess.run(  # noqa: S603 -- fixed arguments, no shell
            [docker, "rm", "-f", container], capture_output=True, timeout=60, check=False
        )


async def run_mailbox_agent(
    server: MailServer,
    inbox: str,
    answer: Callable[[EmailMessage], EmailMessage | None],
    *,
    poll_interval_s: float = 0.2,
) -> NoReturn:
    """A minimal email agent: read new mail to ``inbox`` (IMAP), send ``answer``'s reply (SMTP).

    Runs until cancelled; blocking mail calls run in worker threads.
    """
    seen: set[bytes] = set()
    while True:
        for raw in await anyio.to_thread.run_sync(_fetch_unseen, server, inbox, seen):
            reply = answer(from_mime(raw))
            if reply is not None:
                await anyio.to_thread.run_sync(_send, server, reply)
        await anyio.sleep(poll_interval_s)


def _fetch_unseen(server: MailServer, inbox: str, seen: set[bytes]) -> list[bytes]:
    imap = imaplib.IMAP4(server.host, server.imap_port, timeout=10)
    try:
        imap.login(inbox, inbox)
        imap.select("INBOX", readonly=True)
        _, found = imap.uid("SEARCH", "ALL")
        raws: list[bytes] = []
        for uid in b" ".join(found).split():
            if uid in seen:
                continue
            _, data = imap.uid("FETCH", uid.decode(), "(BODY.PEEK[])")
            raws += [item[1] for item in data if isinstance(item, tuple)]
            seen.add(uid)
        return raws
    finally:
        imap.logout()


def _send(server: MailServer, message: EmailMessage) -> None:
    with smtplib.SMTP(server.host, server.smtp_port, timeout=10) as smtp:
        smtp.send_message(to_mime(message))


def _docker(docker: str, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 -- fixed arguments, no shell
        [docker, *args], capture_output=True, text=True, timeout=300, check=False
    )
    if done.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {done.stderr.strip()}")
    return done.stdout


def _host_port(docker: str, container: str, port: int) -> int:
    mapping = _docker(docker, "port", container, f"{port}/tcp").splitlines()[0]
    return int(mapping.rpartition(":")[2])


def _wait_until_ready(server: MailServer) -> None:
    """Wait for GreenMail's IMAP greeting; Docker publishes ports before the JVM listens."""
    deadline = time.monotonic() + _STARTUP_TIMEOUT_S
    while True:
        try:
            with socket.create_connection((server.host, server.imap_port), timeout=2) as conn:
                if conn.recv(64).startswith(b"* OK"):
                    return
        except OSError:
            pass  # not listening yet; retried until the deadline below
        if time.monotonic() > deadline:
            raise RuntimeError(f"GreenMail did not start within {_STARTUP_TIMEOUT_S} s")
        time.sleep(0.2)
