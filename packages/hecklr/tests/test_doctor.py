"""``hecklr doctor`` on starter projects, with the network replaced where it would be reached."""

import socket
from collections.abc import Callable
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands.doctor import Check, run_checks
from hecklr.probe import AgentProbe

Edit = Callable[[Path, str, str], None]

# Doctor binds a local port to check the world sandbox can start.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

runner = CliRunner()


def _checks(config_path: Path) -> dict[str, Check]:
    return {check.name: check for check in run_checks(config_path)}


def test_starter_project_is_ready(config_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    output = click.unstyle(result.output)
    assert result.exit_code == exit_codes.OK, output
    assert "Ready to run." in output
    checks = _checks(config_path)
    assert checks["python"].status == "ok"
    assert checks["agent (chat)"].detail == "in-process agent hecklr_agent:reply imports"
    assert checks["simulator model"].status == "ok"
    assert {checks["extra [voice]"].status, checks["extra [mine]"].status} <= {"ok", "warn"}


def test_missing_api_key_fails_without_printing_any_secret(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Edit
) -> None:
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setenv("OPENAI_API_KEY", "sk-secret-value")
    edit(config_path, "simulator: fake/customer", "simulator: anthropic/claude-sonnet-5-5")
    edit(config_path, "judge: fake/judge", "judge: openai/gpt-4.1")

    result = runner.invoke(app, ["doctor", "--config", str(config_path)])

    assert result.exit_code == exit_codes.CHECK_FAILED
    output = click.unstyle(result.output)
    assert "ANTHROPIC_API_KEY" in output
    assert "sk-secret-value" not in output
    checks = _checks(config_path)
    assert checks["simulator model"].status == "fail"
    assert checks["judge model"].status == "ok"


def test_ollama_must_run_and_serve_the_models(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Edit
) -> None:
    edit(config_path, "simulator: fake/customer", "simulator: ollama/qwen3:8b")
    served: list[list[str] | None] = [None]
    monkeypatch.setattr("hecklr.commands.doctor.ollama_models", lambda base: served[0])

    assert "ollama serve" in _checks(config_path)["ollama"].detail
    served[0] = ["llama3:8b"]
    assert "ollama pull qwen3:8b" in _checks(config_path)["ollama"].detail
    served[0] = ["qwen3:8b"]
    assert _checks(config_path)["ollama"].status == "ok"


def test_http_agent_reachability(
    config_path: Path, monkeypatch: pytest.MonkeyPatch, edit: Edit
) -> None:
    edit(
        config_path,
        '{"type": "python", "entrypoint": "hecklr_agent:reply"}',
        '{"type": "http", "url": "http://localhost:8000/chat"}',
    )
    probe = AgentProbe("http", "http://localhost:8000/chat", False, "nothing answered")
    monkeypatch.setattr("hecklr.commands.doctor.probe_agent", lambda url: probe)

    check = _checks(config_path)["agent (chat)"]

    assert (check.status, check.detail) == ("fail", "nothing answered")


def test_websocket_agent_reachability(config_path: Path, edit: Edit) -> None:
    with socket.socket() as server:
        server.bind(("127.0.0.1", 0))
        server.listen()
        port = server.getsockname()[1]
        edit(
            config_path,
            '{"type": "python", "entrypoint": "hecklr_agent:reply"}',
            f'{{"type": "websocket", "url": "ws://127.0.0.1:{port}/chat"}}',
        )
        assert _checks(config_path)["agent (chat)"].status == "ok"
    assert _checks(config_path)["agent (chat)"].status == "fail"


def test_broken_python_agent_and_pending_channels(config_path: Path, edit: Edit) -> None:
    edit(config_path, "hecklr_agent:reply", "hecklr_agent:missing")
    edit(
        config_path,
        "  adapters:\n",
        "  adapters:\n    email: { type: smtp, inbox: support@acme.test }\n",
    )

    checks = _checks(config_path)

    assert checks["agent (chat)"].status == "fail"
    assert checks["agent (email)"].status == "warn"


def test_webhook_chat_adapter_is_reported_unavailable(config_path: Path, edit: Edit) -> None:
    edit(
        config_path,
        '{"type": "python", "entrypoint": "hecklr_agent:reply"}',
        '{"type": "webhook", "url": "http://localhost:8000/in"}',
    )

    assert _checks(config_path)["agent (chat)"].status == "fail"


def test_invalid_config_is_reported(tmp_path: Path) -> None:
    result = runner.invoke(app, ["doctor", "--config", str(tmp_path / "missing.yaml")])

    assert result.exit_code == exit_codes.CHECK_FAILED
    assert _checks(tmp_path / "missing.yaml")["config"].status == "fail"
