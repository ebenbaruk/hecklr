"""``hecklr init``: starter projects that validate and run, and agent and model detection."""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.compose import load_run_project
from hecklr.probe import AgentProbe
from hecklr.scaffold import DEFAULT_OLLAMA_MODEL, choose_models
from hecklr_core import resolve_suite
from hecklr_schemas import HttpAdapterConfig, OpenAICompatibleAdapterConfig, PythonAdapterConfig

runner = CliRunner()
FAKE = ["--simulator-model", "fake/customer", "--judge-model", "fake/judge"]


def _init(root: Path, *args: str, stdin: str | None = None) -> tuple[int, str]:
    result = runner.invoke(app, ["init", str(root), *args], input=stdin)
    return result.exit_code, result.output


def test_sc1_init_yes_writes_a_project_with_five_scenarios_and_three_personas(
    tmp_path: Path,
) -> None:
    root = tmp_path / "acme"

    code, output = _init(root, "--yes", *FAKE)

    assert code == exit_codes.OK, output
    project = load_run_project(root / "hecklr.yaml")
    suite = resolve_suite(project, "ci")
    assert len(suite.scenarios) == 5
    assert len(list((root / "personas").glob("*.yaml"))) == 3
    assert len(suite.personas) == 3
    assert project.config.agent.name == "acme"
    assert isinstance(project.config.agent.adapters.chat, PythonAdapterConfig)
    assert (root / "hecklr_agent.py").is_file()
    assert "hecklr doctor && hecklr run" in output


def test_init_refuses_to_overwrite_without_force(tmp_path: Path) -> None:
    _init(tmp_path, "--yes", *FAKE)

    code, output = _init(tmp_path, "--yes", *FAKE)
    assert code == exit_codes.CONFIG_ERROR
    assert "pass --force" in output

    code, _ = _init(tmp_path, "--yes", "--force", *FAKE)
    assert code == exit_codes.OK


def test_init_with_an_explicit_http_adapter_skips_probing(tmp_path: Path) -> None:
    code, output = _init(
        tmp_path, "--yes", "--adapter", "http", "--agent-url", "http://localhost:8000/chat", *FAKE
    )

    assert code == exit_codes.OK, output
    chat = load_run_project(tmp_path / "hecklr.yaml").config.agent.adapters.chat
    assert isinstance(chat, HttpAdapterConfig)
    assert str(chat.url) == "http://localhost:8000/chat"
    assert not (tmp_path / "hecklr_agent.py").exists()


def test_init_probes_the_agent_url_to_pick_the_adapter(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    probe = AgentProbe(
        "openai-compatible", "http://gpu:8000/v1", True, "OpenAI-compatible", ("m1",)
    )
    monkeypatch.setattr("hecklr.commands.init.probe_agent", lambda url: probe)

    code, output = _init(tmp_path, "--yes", "--agent-url", "http://gpu:8000", *FAKE)

    assert code == exit_codes.OK, output
    chat = load_run_project(tmp_path / "hecklr.yaml").config.agent.adapters.chat
    assert isinstance(chat, OpenAICompatibleAdapterConfig)
    assert (str(chat.url), chat.model) == ("http://gpu:8000/v1", "m1")


def test_init_warns_when_nothing_answers(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    probe = AgentProbe("http", "http://localhost:9/chat", False, "nothing answered")
    monkeypatch.setattr("hecklr.commands.init.probe_agent", lambda url: probe)

    code, output = _init(tmp_path, "--yes", "--agent-url", "http://localhost:9/chat", *FAKE)

    assert code == exit_codes.OK
    assert "Start the agent before `hecklr run`" in output


def test_adapter_without_url_is_refused(tmp_path: Path) -> None:
    code, output = _init(tmp_path, "--yes", "--adapter", "http", *FAKE)

    assert code == exit_codes.CONFIG_ERROR
    assert "needs --agent-url" in output


def test_interactive_init_prompts_for_agent_and_models(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr("hecklr.commands.init.ollama_models", lambda: None)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)

    code, output = _init(tmp_path, stdin="\nfake/customer\n\n")

    assert code == exit_codes.OK, output
    models = load_run_project(tmp_path / "hecklr.yaml").config.models
    assert (models.simulator, models.judge) == ("fake/customer", DEFAULT_OLLAMA_MODEL)


def test_invalid_model_names_are_refused_before_writing(tmp_path: Path) -> None:
    code, output = _init(tmp_path, "--yes", "--simulator-model", "gpt4", "--judge-model", "x/y")

    assert code == exit_codes.CONFIG_ERROR
    assert "invalid simulator model 'gpt4'" in output
    assert not (tmp_path / "hecklr.yaml").exists()


def test_agent_name_falls_back_when_the_directory_name_is_unusable(tmp_path: Path) -> None:
    root = tmp_path / "--"
    code, _ = _init(root, "--yes", *FAKE)

    assert code == exit_codes.OK
    assert load_run_project(root / "hecklr.yaml").config.agent.name == "my-agent"


@pytest.mark.parametrize(
    ("env", "ollama", "expected"),
    [
        ({}, ["llama3:8b", "qwen3:8b"], ("ollama/qwen3:8b", "ollama/qwen3:8b")),
        ({}, ["llama3:8b"], ("ollama/llama3:8b", "ollama/llama3:8b")),
        (
            {"ANTHROPIC_API_KEY": "k"},
            None,
            ("anthropic/claude-haiku-5-5", "anthropic/claude-sonnet-5-5"),
        ),
        ({"OPENAI_API_KEY": "k"}, None, ("openai/gpt-4.1-mini", "openai/gpt-4.1")),
        ({}, None, (DEFAULT_OLLAMA_MODEL, DEFAULT_OLLAMA_MODEL)),
    ],
)
def test_models_follow_what_this_machine_has(
    env: dict[str, str], ollama: list[str] | None, expected: tuple[str, str]
) -> None:
    choice = choose_models(env, ollama)

    assert (choice.simulator, choice.judge) == expected
    assert choice.reason
