"""The agent on a real mailbox: GreenMail in Docker, Hecklr's ``smtp-imap`` adapter, a sandbox.

The GreenMail tests are skipped when Docker is unavailable; the failure-path tests need no server.
"""

from collections.abc import Iterator
from contextlib import contextmanager

import anyio
import httpx2
import pytest
import structlog
from acme_email import Mailbox, serve_mailbox
from acme_email.mailbox import answer_raw, fetch_unseen, send
from email_testing import email_desk, load_scenarios, run_served_email_trial

from hecklr_adapters.email import SmtpImapEmailTarget, smtp_imap, to_mime
from hecklr_adapters.email.testing import MailServer, docker_available, greenmail
from hecklr_channels import EmailDriver
from hecklr_schemas import EmailMessage, Persona, World
from hecklr_world import Sandbox

pytestmark = [
    pytest.mark.anyio,
    pytest.mark.enable_socket,
    pytest.mark.allow_hosts(["127.0.0.1"]),
]

SCENARIOS = {s.id: s for s in load_scenarios("scripted/*.yaml")}


@pytest.fixture(scope="module")
def mail_server() -> Iterator[MailServer]:
    """GreenMail in Docker for this module; skipped when Docker is unavailable."""
    if not docker_available():
        pytest.skip("Docker is not available for the GreenMail mail server")
    with greenmail() as server:
        yield server


def mailbox_on(server: MailServer, inbox: str) -> Mailbox:
    return Mailbox(
        inbox=inbox,
        smtp_host=server.host,
        smtp_port=server.smtp_port,
        imap_host=server.host,
        imap_port=server.imap_port,
        poll_interval_s=0.1,
    )


@pytest.mark.slow
@pytest.mark.parametrize(
    ("variant", "scenario_id", "failed"),
    [
        ("clean", "email-reschedule-2", []),
        ("F11", "email-reschedule-2", ["email_threading"]),
        ("F12", "email-timeout-cancel-1", ["timeout", "state"]),
    ],
)
async def test_sc8_mailbox_agent_over_smtp_imap(
    mail_server: MailServer,
    variant: str,
    scenario_id: str,
    failed: list[str],
    personas: dict[str, Persona],
    world: World,
) -> None:
    inbox = f"support.{variant.lower()}@acme.test"  # one inbox per variant: runs never mix
    scenario = SCENARIOS[scenario_id]
    target = SmtpImapEmailTarget(mail_server.config(inbox), poll_interval_s=0.1)
    driver = EmailDriver(target, agent_addr=inbox, reply_timeout_s=4)
    async with Sandbox() as sandbox, anyio.create_task_group() as tasks:
        # The adapter tags each email with X-Hecklr-Trial-Id; the agent finds the trial's world
        # under the sandbox root from it.
        mailbox = mailbox_on(mail_server, inbox)
        desk = email_desk(variant)
        tasks.start_soon(
            lambda: serve_mailbox(desk, world, mailbox, world_base_url=sandbox.base_url)
        )
        persona = personas[scenario.persona_ids[0]]
        trial = await run_served_email_trial(scenario, persona, world, sandbox, driver)
        tasks.cancel_scope.cancel()
    assert trial.failed() == failed, trial.trial.error
    if variant == "clean":
        replies = trial.emails("agent")
        assert len(replies) == 3
        assert all(reply.message.from_addr == inbox for reply in replies)


@pytest.mark.slow
@pytest.mark.parametrize(
    ("variant", "scenario_id", "failed"),
    [("clean", "email-reschedule-2", []), ("F12", "email-timeout-cancel-1", ["timeout", "state"])],
)
async def test_issue70_dropped_reply_polls_change_no_verdict(
    mail_server: MailServer,
    monkeypatch: pytest.MonkeyPatch,
    variant: str,
    scenario_id: str,
    failed: list[str],
    personas: dict[str, Persona],
    world: World,
) -> None:
    """Every third IMAP connection Hecklr opens to look for replies is dropped, as a busy mail
    server (or Docker's port proxy under CI load) may do. The clean agent still passes, and F12's
    hung tool is still a timeout, never an agent error (issue #70: one dropped poll in the
    4 s wait for F12's reply failed the trial as ``agent_error``)."""
    connect = smtp_imap._imap
    connections = 0

    @contextmanager
    def dropping(host: str, port: int, timeout_s: float) -> Iterator[object]:
        nonlocal connections
        connections += 1
        if connections % 3 == 0:
            raise ConnectionResetError("connection reset by peer")
        with connect(host, port, timeout_s) as imap:
            yield imap

    monkeypatch.setattr(smtp_imap, "_imap", dropping)
    inbox = f"support.drops.{variant.lower()}@acme.test"
    scenario = SCENARIOS[scenario_id]
    target = SmtpImapEmailTarget(mail_server.config(inbox), poll_interval_s=0.1)
    driver = EmailDriver(target, agent_addr=inbox, reply_timeout_s=4)
    async with Sandbox() as sandbox, anyio.create_task_group() as tasks:
        mailbox = mailbox_on(mail_server, inbox)
        desk = email_desk(variant)
        tasks.start_soon(
            lambda: serve_mailbox(desk, world, mailbox, world_base_url=sandbox.base_url)
        )
        persona = personas[scenario.persona_ids[0]]
        trial = await run_served_email_trial(scenario, persona, world, sandbox, driver)
        tasks.cancel_scope.cancel()
    assert connections >= 3, connections  # at least one poll was dropped
    assert trial.failed() == failed, trial.trial.error


@pytest.mark.slow
async def test_mail_is_answered_once_even_across_polls(mail_server: MailServer) -> None:
    inbox = "support.once@acme.test"
    mailbox = mailbox_on(mail_server, inbox)
    email = EmailMessage(
        message_id="<once-1@customers.hecklr.test>",
        subject="Hello",
        body="Hello",
        from_addr="once@customers.hecklr.test",
        to_addrs=[inbox],
    )
    await anyio.to_thread.run_sync(send, mailbox, to_mime(email))
    first = await anyio.to_thread.run_sync(fetch_unseen, mailbox)
    second = await anyio.to_thread.run_sync(fetch_unseen, mailbox)
    assert len(first) == 1
    assert second == []


async def test_an_unreachable_mailbox_is_logged_and_polled_again(world: World) -> None:
    mailbox = Mailbox(inbox="x@acme.test", imap_port=9, poll_interval_s=0.05, timeout_s=1)
    with structlog.testing.capture_logs() as logs, anyio.move_on_after(0.5):
        await serve_mailbox(email_desk(), world, mailbox)
    polls = [log for log in logs if log["event"] == "mailbox_poll_failed"]
    assert len(polls) >= 2


async def test_an_email_that_cannot_be_answered_is_logged(world: World) -> None:
    mailbox = Mailbox(inbox="x@acme.test", smtp_port=9, timeout_s=1)
    raw = to_mime(
        EmailMessage(
            message_id="<2@customers.hecklr.test>",
            subject="Hello",
            body="Hello",
            from_addr="c@customers.hecklr.test",
            to_addrs=["x@acme.test"],
        )
    ).as_bytes()
    async with httpx2.AsyncClient() as client:
        with structlog.testing.capture_logs() as logs:
            await answer_raw(
                email_desk(), raw, mailbox=mailbox, world=world, client=client, world_base_url=None
            )
    assert [log["event"] for log in logs] == ["email_reply_failed"]
