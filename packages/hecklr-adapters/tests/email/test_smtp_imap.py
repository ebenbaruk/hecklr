"""The smtp-imap email adapter: configuration, failure mapping, and a real GreenMail round trip."""

import socket
from collections.abc import Iterator
from contextlib import contextmanager
from typing import Any

import anyio
import pytest

from hecklr_adapters import AdapterConfigError, AgentError, AgentTimeoutError, make_email_target
from hecklr_adapters.email import SmtpImapEmailTarget, make_reply, smtp_imap
from hecklr_adapters.email.testing import MailServer, run_mailbox_agent
from hecklr_schemas import (
    EmailMessage,
    EmailTarget,
    SessionContext,
    SmtpImapAdapterConfig,
    TrialId,
    new_id,
)

pytestmark = pytest.mark.anyio

AGENT = "support@acme.test"


def config(**changes: Any) -> SmtpImapAdapterConfig:
    return SmtpImapAdapterConfig.model_validate({"type": "smtp-imap", "inbox": AGENT} | changes)


def ctx(timeout_s: float = 5) -> SessionContext:
    return SessionContext(trial_id=TrialId(new_id()), seed=1, timeout_s=timeout_s)


def email(sender: str | None = None, body: str = "Move my visit") -> EmailMessage:
    return EmailMessage(
        message_id=f"<{new_id()}@customers.hecklr.test>", subject="Visit", body=body,
        from_addr=sender or f"c.{new_id().lower()}@customers.hecklr.test", to_addrs=[AGENT],
    )  # fmt: skip


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


# --- configuration ----------------------------------------------------------------------------


def test_ws12_smtp_imap_credentials_need_both_variables() -> None:
    with pytest.raises(AdapterConfigError, match="both username_env and password_env"):
        SmtpImapEmailTarget(config(username_env="MAIL_USER"))


def test_ws12_smtp_imap_credentials_must_be_set(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MAIL_USER", "u")
    monkeypatch.delenv("MAIL_PASS", raising=False)
    with pytest.raises(AdapterConfigError, match="MAIL_PASS is not set"):
        make_email_target(config(username_env="MAIL_USER", password_env="MAIL_PASS"))


def test_ws12_smtp_alias_builds_the_smtp_imap_target() -> None:
    target = make_email_target(config(type="smtp"))
    assert isinstance(target, SmtpImapEmailTarget)
    assert isinstance(target, EmailTarget)


# --- failures ---------------------------------------------------------------------------------


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws12_unreachable_smtp_server_is_an_agent_error() -> None:
    target = SmtpImapEmailTarget(config(smtp_host="127.0.0.1", smtp_port=closed_port()))
    with pytest.raises(AgentError, match=r"SMTP server 127\.0\.0\.1:\d+ failed"):
        await target.deliver(email(), ctx())


@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws12_unreachable_imap_server_fails_the_reply_stream() -> None:
    port = closed_port()
    target = SmtpImapEmailTarget(config(imap_host="127.0.0.1", imap_port=port))
    context = ctx()
    target._addresses.add(context.trial_id, "c@customers.hecklr.test")
    with pytest.raises(AgentError, match="IMAP server"):
        await anext(target.replies(context))


async def test_ws12_a_silent_mail_server_is_a_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class Silent:
        def __init__(self, *args: object, **kwargs: object) -> None:
            raise TimeoutError

    monkeypatch.setattr("smtplib.SMTP", Silent)
    with pytest.raises(AgentTimeoutError, match="did not answer within 5"):
        await SmtpImapEmailTarget(config()).deliver(email(), ctx())


class FakeImap:
    """Scripted IMAP responses, for the failure branches a healthy server never takes."""

    def __init__(self, fetch: list[Any], search_status: str = "OK") -> None:
        self.fetch = fetch
        self.search_status = search_status

    def login(self, user: str, password: str) -> tuple[str, list[bytes]]:
        return "OK", [b"done"]

    def select(self, mailbox: str, readonly: bool) -> tuple[str, list[bytes]]:
        return "OK", [b"1"]

    def uid(self, command: str, *args: str) -> tuple[str, list[Any]]:
        if command == "SEARCH":
            return self.search_status, [b"7"]
        return "OK", self.fetch


def use_imap(monkeypatch: pytest.MonkeyPatch, fake: FakeImap) -> None:
    @contextmanager
    def fake_imap(host: str, port: int, timeout_s: float) -> Iterator[FakeImap]:
        yield fake

    monkeypatch.setattr(smtp_imap, "_imap", fake_imap)


async def test_ws12_a_failed_imap_command_is_an_agent_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    use_imap(monkeypatch, FakeImap([], search_status="NO"))
    target = SmtpImapEmailTarget(config())
    context = ctx()
    target._addresses.add(context.trial_id, "c@customers.hecklr.test")
    with pytest.raises(AgentError, match="IMAP command failed: NO"):
        await anext(target.replies(context))


async def test_ws12_an_empty_imap_fetch_is_an_agent_error(monkeypatch: pytest.MonkeyPatch) -> None:
    use_imap(monkeypatch, FakeImap([b")"]))
    target = SmtpImapEmailTarget(config())
    context = ctx()
    target._addresses.add(context.trial_id, "c@customers.hecklr.test")
    with pytest.raises(AgentError, match="FETCH of UID 7 returned no message"):
        await anext(target.replies(context))


# --- against a real mail server ---------------------------------------------------------------


@pytest.mark.slow
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws12_smtp_imap_round_trip_through_greenmail(mail_server: MailServer) -> None:
    received: list[EmailMessage] = []

    def answer(msg: EmailMessage) -> EmailMessage:
        received.append(msg)
        return make_reply(msg, body=f"Re-{msg.body}", from_addr=AGENT)

    target = SmtpImapEmailTarget(mail_server.config(AGENT), poll_interval_s=0.1)
    context = ctx()
    sent = email()
    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_mailbox_agent, mail_server, AGENT, answer)
        replies = target.replies(context)
        await target.deliver(sent, context)
        with anyio.fail_after(30):
            reply = await anext(replies)
        await replies.aclose()  # type: ignore[attr-defined]
        tasks.cancel_scope.cancel()

    assert (reply.in_reply_to, reply.references) == (sent.message_id, [sent.message_id])
    assert reply.body == "Re-Move my visit"
    assert received[0].message_id == sent.message_id
    assert received[0].body == sent.body
    assert target._addresses.of(context.trial_id) == frozenset()  # forgotten once closed


@pytest.mark.slow
@pytest.mark.enable_socket
@pytest.mark.allow_hosts(["127.0.0.1"])
async def test_ws12_smtp_imap_with_credentials_searches_by_recipient(
    mail_server: MailServer, monkeypatch: pytest.MonkeyPatch
) -> None:
    customer = f"c.{new_id().lower()}@customers.hecklr.test"
    # GreenMail ignores passwords; logging in as the customer stands in for a catch-all account.
    monkeypatch.setenv("MAIL_USER", customer)
    monkeypatch.setenv("MAIL_PASS", "secret")
    creds = {"username_env": "MAIL_USER", "password_env": "MAIL_PASS"}
    target = SmtpImapEmailTarget(mail_server.config(AGENT, **creds), poll_interval_s=0.1)
    sent = email(sender=customer)
    context = ctx()

    def answer(msg: EmailMessage) -> EmailMessage:
        return make_reply(msg, body="ok", from_addr=AGENT)

    async with anyio.create_task_group() as tasks:
        tasks.start_soon(run_mailbox_agent, mail_server, AGENT, answer)
        await target.deliver(sent, context)
        replies = target.replies(context)
        with anyio.fail_after(30):
            reply = await anext(replies)
        await replies.aclose()  # type: ignore[attr-defined]
        tasks.cancel_scope.cancel()
    assert reply.to_addrs == [customer]
