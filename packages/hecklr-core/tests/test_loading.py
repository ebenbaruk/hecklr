"""Run lifecycle step 1: load, validate with file and line, cross-check, filter, hash."""

import textwrap
from pathlib import Path

import pytest

from hecklr_core import (
    ConfigError,
    Project,
    config_hash,
    load_model,
    load_project,
    load_yaml,
    resolve_suite,
)
from hecklr_schemas import Channel, Scenario


def _write(path: Path, text: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(textwrap.dedent(text), encoding="utf-8")
    return path


# --- YAML ---------------------------------------------------------------------------------------


def test_iso_timestamps_stay_strings(tmp_path: Path) -> None:
    doc = load_yaml(_write(tmp_path / "a.yaml", "slot: 2026-10-23T09:00\nday: 2026-10-23\n"))
    assert doc.data == {"slot": "2026-10-23T09:00", "day": "2026-10-23"}


def test_validation_error_names_file_and_line_of_the_bad_field(tmp_path: Path) -> None:
    path = _write(
        tmp_path / "s.yaml",
        """\
        id: s1
        title: t
        channels: [chat]
        persona: p
        world: w
        goal: g
        success:
          - type: contains
            text: hi
          - type: tool_called
            tooll: reschedule
        """,
    )
    with pytest.raises(ConfigError) as caught:
        load_model(path, Scenario)
    lines = {issue.line for issue in caught.value.issues}
    # The typo'd key is on line 11 and the missing ``tool`` belongs to the item starting line 10.
    assert lines == {10, 11}
    assert all(issue.file == path for issue in caught.value.issues)
    assert f"{path}:11: success.1.tool_called.tooll" in str(caught.value)


def test_missing_top_level_field_points_at_the_document(tmp_path: Path) -> None:
    path = _write(tmp_path / "s.yaml", "id: s1\n")
    with pytest.raises(ConfigError) as caught:
        load_model(path, Scenario)
    assert {issue.line for issue in caught.value.issues} == {1}


@pytest.mark.parametrize(
    ("text", "line", "message"),
    [
        ("a: [1, 2\n", 2, "invalid YAML"),
        ("a: 1\nb: 2\na: 3\n", 3, "duplicate key 'a' (first on line 1)"),
        ("", None, "file is empty"),
        ("a: 1\n---\nb: 2\n", 2, "invalid YAML"),
        ("a: \x07\n", None, "invalid YAML"),
    ],
)
def test_malformed_yaml_is_located(
    tmp_path: Path, text: str, line: int | None, message: str
) -> None:
    path = _write(tmp_path / "bad.yaml", text)
    with pytest.raises(ConfigError) as caught:
        load_yaml(path)
    (issue,) = caught.value.issues
    assert issue.line == line
    assert message in issue.message


def test_missing_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(ConfigError, match="cannot read file"):
        load_yaml(tmp_path / "nope.yaml")


def test_non_utf8_file_is_a_config_error(tmp_path: Path) -> None:
    path = tmp_path / "latin1.yaml"
    path.write_bytes("goal: déplacer\n".encode("latin-1"))
    with pytest.raises(ConfigError, match="not UTF-8 text"):
        load_yaml(path)


def test_config_error_requires_issues() -> None:
    with pytest.raises(ValueError, match="at least one issue"):
        ConfigError([])


# --- project and suites -------------------------------------------------------------------------


def test_load_project_reads_config_and_world(project: Project, project_dir: Path) -> None:
    assert project.config.agent.name == "acme-support"
    assert project.world.id == "acme"
    assert project.root == project_dir.resolve()
    assert project.output_dir == project_dir.resolve() / ".hecklr"
    # The world seed record keeps its ISO slot as a string.
    assert project.world.collections["appointments"].records[0]["slot"] == "2026-10-23T09:00"


def test_load_project_reports_a_bad_world_file(project_dir: Path) -> None:
    _write(project_dir / "worlds/acme.yaml", "id: acme\ncollections: 3\n")
    with pytest.raises(ConfigError) as caught:
        load_project(project_dir / "hecklr.yaml")
    assert caught.value.issues[0].file.name == "acme.yaml"
    assert caught.value.issues[0].line == 2


def test_suite_filters_by_tags_and_channels(project: Project) -> None:
    suite = resolve_suite(project, "ci")
    assert [cell.scenario.id for cell in suite.cells] == ["reschedule"]
    (cell,) = suite.cells
    assert cell.persona_ids == ("busy", "calm")
    assert cell.channels == (Channel.CHAT,)
    assert suite.trials == 2
    assert set(suite.personas) == {"busy", "calm"}


def test_suite_without_filters_keeps_configured_adapter_channels(project: Project) -> None:
    suite = resolve_suite(project, "all")
    channels = {cell.scenario.id: cell.channels for cell in suite.cells}
    # voice is listed by the scenario but the agent has no voice adapter.
    assert channels == {"invoice": (Channel.CHAT,), "reschedule": (Channel.CHAT, Channel.EMAIL)}


def test_suite_overrides_trials_and_channels(project: Project) -> None:
    suite = resolve_suite(project, "all", trials=4, channels=[Channel.EMAIL])
    assert suite.trials == 4
    assert [(cell.scenario.id, cell.channels) for cell in suite.cells] == [
        ("reschedule", (Channel.EMAIL,))
    ]
    with pytest.raises(ValueError, match="trials"):
        resolve_suite(project, "all", trials=0)


def test_suite_personas_glob_crosses_every_scenario(project: Project) -> None:
    suite = resolve_suite(project, "redteam")
    assert {cell.persona_ids for cell in suite.cells} == {("fraud",)}
    assert set(suite.personas) == {"fraud"}


def test_unknown_suite_lists_the_known_ones(project: Project) -> None:
    with pytest.raises(ConfigError, match="unknown suite 'nightly'; defined: all, ci, redteam"):
        resolve_suite(project, "nightly")


def test_requested_channel_without_adapter_is_refused(project: Project) -> None:
    with pytest.raises(ConfigError, match=r"channels \['voice'\]"):
        resolve_suite(project, "all", channels=[Channel.VOICE])


def test_unknown_persona_reference_names_the_scenario_file(project_dir: Path) -> None:
    _write(
        project_dir / "scenarios/billing/invoice.yaml",
        """\
        id: invoice
        title: t
        channels: [chat]
        persona: ghost
        world: acme
        goal: g
        success: [{ type: contains, text: x }]
        """,
    )
    with pytest.raises(ConfigError) as caught:
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")
    (issue,) = caught.value.issues
    assert issue.file.name == "invoice.yaml"
    assert "unknown persona 'ghost'" in issue.message


def test_scenario_for_another_world_is_refused(project_dir: Path) -> None:
    text = (project_dir / "scenarios/billing/invoice.yaml").read_text()
    _write(project_dir / "scenarios/billing/invoice.yaml", text.replace("world: acme", "world: x"))
    with pytest.raises(ConfigError, match="needs world 'x', but the project's world is 'acme'"):
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")


def test_adversarial_scenario_needs_adversarial_personas(project_dir: Path) -> None:
    _write(
        project_dir / "scenarios/attack.yaml",
        """\
        id: attack
        title: t
        channels: [chat]
        persona: [fraud, calm]
        world: acme
        goal: g
        mode: adversarial
        success: [{ type: no_unauthorized_refund }]
        """,
    )
    with pytest.raises(ConfigError) as caught:
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")
    (issue,) = caught.value.issues
    assert "persona 'calm', which has no adversarial strategy" in issue.message


def test_voice_cell_needs_a_voice_profile(project_dir: Path) -> None:
    config = (project_dir / "hecklr.yaml").read_text()
    _write(
        project_dir / "hecklr.yaml",
        config.replace(
            "    email: { type: smtp, inbox: support@acme.test }",
            "    email: { type: smtp, inbox: support@acme.test }\n"
            "    voice: { type: pipecat, url: http://localhost:7860/offer }",
        ),
    )
    with pytest.raises(ConfigError, match="persona 'busy' has no voice profile"):
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")


def test_duplicate_ids_across_files_are_refused(project_dir: Path) -> None:
    _write(project_dir / "personas/copy.yaml", "id: calm\ndescription: d\n")
    with pytest.raises(ConfigError, match="id 'calm' is also defined in"):
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")


def test_every_invalid_file_is_reported_at_once(project_dir: Path) -> None:
    _write(project_dir / "scenarios/x.yaml", "id: x\n")
    _write(project_dir / "scenarios/y.yaml", "id: y\n")
    with pytest.raises(ConfigError) as caught:
        resolve_suite(load_project(project_dir / "hecklr.yaml"), "all")
    assert {issue.file.name for issue in caught.value.issues} == {"x.yaml", "y.yaml"}


def test_empty_selection_and_empty_persona_glob_are_refused(project_dir: Path) -> None:
    config = (project_dir / "hecklr.yaml").read_text()
    _write(
        project_dir / "hecklr.yaml",
        config.replace("tags: [core]", "tags: [nothing]").replace(
            "personas/adversarial/*.yaml", "personas/none/*.yaml"
        ),
    )
    project = load_project(project_dir / "hecklr.yaml")
    with pytest.raises(ConfigError, match="suite 'ci' selects no scenario"):
        resolve_suite(project, "ci")
    with pytest.raises(ConfigError, match="personas glob matches no file"):
        resolve_suite(project, "redteam")


def test_config_hash_is_stable_and_content_sensitive(project_dir: Path) -> None:
    first = resolve_suite(load_project(project_dir / "hecklr.yaml"), "ci").config_hash
    again = resolve_suite(load_project(project_dir / "hecklr.yaml"), "ci").config_hash
    assert first == again
    assert len(first) == 64
    text = (project_dir / "personas/busy.yaml").read_text()
    _write(project_dir / "personas/busy.yaml", text.replace("Busy", "Very busy"))
    changed = resolve_suite(load_project(project_dir / "hecklr.yaml"), "ci").config_hash
    assert changed != first


def test_config_hash_ignores_scenario_order(project: Project) -> None:
    suite = resolve_suite(project, "all")
    scenarios = [cell.scenario for cell in suite.cells]
    personas = list(suite.personas.values())
    forward = config_hash(project.config, project.world, scenarios, personas)
    backward = config_hash(project.config, project.world, scenarios[::-1], personas[::-1])
    assert forward == backward
    assert forward != config_hash(
        project.config, project.world, scenarios, personas, extra={"trials": 9}
    )
