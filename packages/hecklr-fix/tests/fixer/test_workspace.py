from datetime import UTC, datetime
from pathlib import Path

import pytest
import yaml

from hecklr_fix.errors import FixError
from hecklr_fix.target import FixTarget
from hecklr_fix.workspace import TARGETED_DIR, TARGETED_SUITE, prepare_workspace, with_port
from hecklr_schemas import Channel, ClusterId, FailureCluster, Scenario, new_id

from .factories import scenario

TARGET = FixTarget(
    cluster=FailureCluster(
        id=ClusterId(new_id()), label="x", source="run", created_at=datetime.now(UTC)
    ),
    scenarios=(scenario("book-a"), scenario("book-b")),
    channels=(Channel.CHAT,),
    personas="personas/*.yaml",
)


def test_the_copy_holds_the_candidate_files_targeted_suite_and_agent_port(
    project: Path, tmp_path: Path
) -> None:
    patched = "Rules:\n- Confirm the slot.\n"

    workspace = prepare_workspace(
        config_path=project,
        dest=tmp_path / "copy",
        files={"prompts/system.md": patched},
        port=43210,
        target=TARGET,
        targeted_trials=3,
    )

    root = workspace.root
    assert (root / "prompts" / "system.md").read_text() == patched
    assert (root / "agent" / "serve.py").is_file()
    assert not (root / ".hecklr").exists()
    assert not (root / ".git").exists()
    assert not (root / "agent" / "__pycache__").exists()
    config = yaml.safe_load(workspace.config_path.read_text())
    assert config["agent"]["adapters"]["chat"]["url"] == "http://127.0.0.1:43210/chat/stream"
    assert config["suites"][TARGETED_SUITE] == {
        "scenarios": f"{TARGETED_DIR}/*.yaml",
        "trials": 3,
        "channels": ["chat"],
        "personas": "personas/*.yaml",
    }
    assert config["suites"]["ci"]["trials"] == 2
    copied = {p.name for p in (root / TARGETED_DIR).iterdir()}
    assert copied == {"book-a.yaml", "book-b.yaml"}
    loaded = Scenario.model_validate(
        yaml.safe_load((root / TARGETED_DIR / "book-a.yaml").read_text())
    )
    assert loaded == TARGET.scenarios[0]
    assert workspace.agent_host == "127.0.0.1"
    # The user's project is untouched.
    assert "first free slot" in (project.parent / "prompts" / "system.md").read_text()


def test_guardrail_a_symlinked_editable_file_is_replaced_not_written_through(
    project: Path, tmp_path: Path
) -> None:
    shared = tmp_path / "shared.md"
    shared.write_text("shared\n")
    prompt = project.parent / "prompts" / "system.md"
    prompt.unlink()
    prompt.symlink_to(shared)

    workspace = prepare_workspace(
        config_path=project,
        dest=tmp_path / "copy",
        files={"prompts/system.md": "patched\n"},
        port=1,
        target=TARGET,
        targeted_trials=1,
    )

    assert shared.read_text() == "shared\n"
    assert (workspace.root / "prompts" / "system.md").read_text() == "patched\n"


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("http://localhost:8000/chat", ("http://localhost:9/chat", "localhost")),
        ("ws://agent/ws?x=1", ("ws://agent:9/ws?x=1", "agent")),
        ("http://[::1]:80/c", ("http://[::1]:9/c", "::1")),
    ],
)
def test_adapter_urls_get_the_agent_port(url: str, expected: tuple[str, str]) -> None:
    assert with_port(url, 9) == expected


def test_an_agent_without_url_cannot_be_driven(project: Path, tmp_path: Path) -> None:
    text = project.read_text().replace(
        "chat: {type: http, url: 'http://127.0.0.1:8000/chat/stream', stream: sse}",
        "chat: {type: python, entrypoint: 'agent:chat'}",
    )
    project.write_text(text)

    with pytest.raises(FixError, match=r"no adapter in hecklr\.yaml has a url"):
        prepare_workspace(
            config_path=project,
            dest=tmp_path / "copy",
            files={},
            port=1,
            target=TARGET,
            targeted_trials=1,
        )


def test_an_unreadable_config_is_a_fix_error(project: Path, tmp_path: Path) -> None:
    project.write_text("- just\n- a list\n")

    with pytest.raises(FixError, match="is not a mapping"):
        prepare_workspace(
            config_path=project,
            dest=tmp_path / "copy",
            files={},
            port=1,
            target=TARGET,
            targeted_trials=1,
        )
