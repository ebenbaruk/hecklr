"""``mailer.yaml``, and fault overlays that replace it (F11, F12) or a brain file (F1-F6)."""

import difflib
from pathlib import Path

import pytest
from acme_agent import AgentConfigError, fault_overlay, load_agent_files
from acme_email import EMAIL_ROOT, MAILER_FILE, MailerSettings, load_email_agent_files, load_mailer

EMAIL_FAULTS = {
    "F11": {"thread_replies": False},
    "F12": {"tool_timeout_s": None},
}


def test_clean_mailer_threads_and_has_a_tool_deadline() -> None:
    files = load_email_agent_files()
    assert files.variant == "clean"
    assert files.mailer == MailerSettings(thread_replies=True, tool_timeout_s=5.0)
    assert files.brain == load_agent_files()


@pytest.mark.parametrize("fault", sorted(EMAIL_FAULTS))
def test_sc8_email_fault_changes_exactly_one_line_of_mailer_yaml(fault: str) -> None:
    overlay = fault_overlay(fault)
    assert sorted(path.name for path in overlay.iterdir()) == ["README.md", "mailer.yaml"]
    clean = (EMAIL_ROOT / MAILER_FILE).read_text().splitlines()
    faulty = (overlay / MAILER_FILE).read_text().splitlines()
    changed = [
        line
        for line in difflib.unified_diff(clean, faulty, lineterm="", n=0)
        if line[:1] in "+-" and not line.startswith(("+++", "---"))
    ]
    assert len(changed) == 2, changed


@pytest.mark.parametrize(("fault", "switch"), sorted(EMAIL_FAULTS.items()))
def test_sc8_email_fault_flips_exactly_its_setting(fault: str, switch: dict[str, object]) -> None:
    files = load_email_agent_files(fault_overlay(fault))
    clean = load_email_agent_files()
    assert files.variant == fault
    assert files.brain == clean.brain  # the brain files stay clean
    assert files.mailer == clean.mailer.model_copy(update=switch)


def test_a_brain_fault_keeps_the_clean_mailer() -> None:
    files = load_email_agent_files(fault_overlay("F3"))
    assert files.variant == "F3"
    assert files.brain == load_agent_files(overlay=fault_overlay("F3"))
    assert files.mailer == load_email_agent_files().mailer


def test_a_mailer_overlay_may_not_change_a_brain_file_too(tmp_path: Path) -> None:
    (tmp_path / "prompts").mkdir()
    (tmp_path / "prompts" / "system.md").write_text("Be terse.")
    (tmp_path / "mailer.yaml").write_text("thread_replies: false\n")
    with pytest.raises(AgentConfigError, match="a fault changes one file"):
        load_email_agent_files(tmp_path)


def test_the_chat_agent_refuses_an_email_fault() -> None:
    # F11/F12 are about mail: the chat agent's loader must not silently run them as clean.
    with pytest.raises(AgentConfigError, match="may only replace editable files"):
        load_agent_files(overlay=fault_overlay("F11"))


@pytest.mark.parametrize(
    "text", ["thread_replies: maybe\n", "tool_timeout_s: 0\n", "colour: blue\n", "[unclosed\n"]
)
def test_a_bad_mailer_file_is_a_config_error(tmp_path: Path, text: str) -> None:
    path = tmp_path / "mailer.yaml"
    path.write_text(text)
    with pytest.raises(AgentConfigError, match="cannot read the mailer settings"):
        load_mailer(path)


def test_an_empty_mailer_file_means_the_defaults(tmp_path: Path) -> None:
    path = tmp_path / "mailer.yaml"
    path.write_text("# nothing set\n")
    assert load_mailer(path) == MailerSettings()


def test_a_missing_mailer_file_is_a_config_error(tmp_path: Path) -> None:
    with pytest.raises(AgentConfigError):
        load_email_agent_files(email_root=tmp_path)
