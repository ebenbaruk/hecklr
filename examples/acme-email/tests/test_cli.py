"""The command line and the in-process agent's default tool backends."""

from pathlib import Path

import pytest
from acme_agent import HttpWorldTools, LocalWorldTools
from acme_email import InProcessEmailAgent
from acme_email.__main__ import mailbox_from, main, parse_args, settings_from
from email_testing import email_desk

from hecklr_adapters import AgentSession
from hecklr_schemas import SessionContext, TrialId, World, new_id


def test_webhook_flags_override_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_FAULT", "F3")
    monkeypatch.setenv("ACME_BRAIN", "llm")
    args = parse_args(["--brain", "rules", "--fault", "F11", "webhook", "--port", "9100"])
    settings = settings_from(args)
    assert (args.transport, args.host, args.port) == ("webhook", "127.0.0.1", 9100)
    assert (settings.brain, settings.fault, settings.overlay) == ("rules", "F11", None)


def test_an_overlay_flag_replaces_an_environment_fault(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setenv("ACME_FAULT", "F3")
    settings = settings_from(parse_args(["--overlay", str(tmp_path), "webhook"]))
    assert (settings.fault, settings.overlay) == (None, tmp_path.resolve())


def test_mailbox_flags_and_credentials(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_MAIL_USERNAME", "agent")
    monkeypatch.setenv("ACME_MAIL_PASSWORD", "secret")
    args = parse_args(["mailbox", "--inbox", "help@acme.test", "--imap-port", "3143"])
    mailbox = mailbox_from(args)
    assert (mailbox.inbox, mailbox.imap_port, mailbox.smtp_port) == ("help@acme.test", 3143, 1025)
    assert (mailbox.username, mailbox.password) == ("agent", "secret")


def test_an_unknown_fault_exits_with_a_config_error(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--brain", "rules", "--fault", "F99", "webhook"]) == 2
    assert "no fault variant 'F99'" in capsys.readouterr().err


@pytest.mark.anyio
async def test_the_in_process_agent_uses_the_trial_sandbox_when_it_has_one(world: World) -> None:
    trial_id = TrialId(new_id())
    with_world = SessionContext(trial_id=trial_id, seed=1, world_http_url="http://w/t/1")
    without = SessionContext(trial_id=trial_id, seed=1)
    async with InProcessEmailAgent(email_desk(), world) as agent:
        remote = agent._default_backend(AgentSession(id="s1", context=with_world))
        local = agent._default_backend(AgentSession(id="s2", context=without))
    assert isinstance(remote, HttpWorldTools)
    assert isinstance(local, LocalWorldTools)
