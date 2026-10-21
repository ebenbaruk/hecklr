"""Settings, agent files and the command line."""

from datetime import date
from pathlib import Path

import pytest
from acme_agent import AgentConfigError, AgentSettings, load_agent_files
from acme_agent.__main__ import main, parse_args, settings_from
from acme_agent.config import DEFAULT_MODEL, TODAY, ToolSpec, fault_overlay


def test_defaults_run_the_clean_llm_agent() -> None:
    settings = AgentSettings.from_env({})
    assert settings.brain == "llm"
    assert settings.model == DEFAULT_MODEL == "ollama/mistral:latest"
    assert settings.today == TODAY
    assert settings.overlay_dir() is None
    assert load_agent_files().variant == "clean"


def test_environment_overrides() -> None:
    settings = AgentSettings.from_env(
        {
            "ACME_BRAIN": "rules",
            "ACME_MODEL": "ollama/qwen3:8b",
            "ACME_OVERLAY": "/tmp/x",  # noqa: S108 -- a path value, never opened
            "ACME_WORLD_BASE_URL": "http://127.0.0.1:8765",
            "ACME_TODAY": "2026-11-01",
            "ACME_SEED": "3",
            "ACME_TOOL_TIMEOUT_S": "2.5",
        }
    )
    assert settings.brain == "rules"
    assert settings.overlay_dir() == Path("/tmp/x")  # noqa: S108 -- as above
    assert settings.world_base_url == "http://127.0.0.1:8765"
    assert settings.today == date(2026, 11, 1)
    assert (settings.seed, settings.tool_timeout_s) == (3, 2.5)


@pytest.mark.parametrize(
    ("env", "message"),
    [
        ({"ACME_BRAIN": "magic"}, "ACME_BRAIN"),
        ({"ACME_TODAY": "tomorrow"}, "invalid ACME_"),
        ({"ACME_SEED": "x"}, "invalid ACME_"),
    ],
)
def test_bad_environment_is_refused(env: dict[str, str], message: str) -> None:
    with pytest.raises(AgentConfigError, match=message):
        AgentSettings.from_env(env)


def test_flags_override_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ACME_FAULT", "F1")
    monkeypatch.setenv("ACME_BRAIN", "llm")
    settings = settings_from(parse_args(["--brain", "rules", "--fault", "F5", "--port", "9000"]))
    assert (settings.brain, settings.fault) == ("rules", "F5")
    overlay = settings_from(parse_args(["--overlay", "/tmp/o"]))  # noqa: S108 -- never opened
    assert (overlay.fault, overlay.overlay) == (None, Path("/tmp/o"))  # noqa: S108 -- as above
    assert settings_from(parse_args([])).fault == "F1"


def test_main_reports_a_bad_fault_without_starting(capsys: pytest.CaptureFixture[str]) -> None:
    assert main(["--brain", "rules", "--fault", "F42"]) == 2
    assert "no fault variant 'F42'" in capsys.readouterr().err


def _write_catalog(root: Path, text: str) -> None:
    (root / "prompts").mkdir()
    (root / "tools").mkdir()
    (root / "prompts" / "system.md").write_text("prompt")
    (root / "tools" / "descriptions.yaml").write_text(text)


@pytest.mark.parametrize(
    ("catalog", "message"),
    [
        ("tools: {}", "non-empty 'tools'"),
        ("tools: {a: {}}", "needs a description"),
        ("tools: {a: {description: x, parameters: {type: string}}}", "object schema"),
        ("tools: [", "cannot read"),
    ],
)
def test_malformed_catalogs_are_refused(tmp_path: Path, catalog: str, message: str) -> None:
    _write_catalog(tmp_path, catalog)
    with pytest.raises(AgentConfigError, match=message):
        load_agent_files(root=tmp_path)


def test_a_minimal_catalog_loads(tmp_path: Path) -> None:
    _write_catalog(tmp_path, "tools: {ping: {description: '  Ping\n the   world. '}}")
    files = load_agent_files(root=tmp_path)
    assert files.tool("ping") == ToolSpec(
        name="ping", description="Ping the world.", parameters={"type": "object", "properties": {}}
    )
    assert files.tool("ping").parameter_description("x") == ""
    with pytest.raises(AgentConfigError, match="no tool 'pong'"):
        files.tool("pong")


def test_an_overlay_must_be_a_directory(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError, match="not a directory"):
        load_agent_files(overlay=tmp_path / "missing")


def test_fault_overlays_resolve_inside_a_given_root(tmp_path: Path) -> None:
    (tmp_path / "F7").mkdir()
    assert fault_overlay("F7", faults_root=tmp_path) == tmp_path / "F7"
