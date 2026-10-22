"""The email front end's settings (``mailer.yaml``) and how a fault variant overlays them.

The agent's brain files (system prompt, tool catalog) are the chat agent's
(``examples/acme-maintenance``). The email front end adds one file of its own, ``mailer.yaml``. A
fault variant is an overlay directory that replaces exactly one of these files (ADR 0008a): F1-F6
replace a brain file, F11 and F12 replace ``mailer.yaml``. So ``--fault F12`` changes one line of
one file and no code path, whichever file it is.
"""

from dataclasses import dataclass
from pathlib import Path
from typing import Final

import yaml
from acme_agent.config import EDITABLE, OVERLAY_DOCS, AgentConfigError, AgentFiles, load_agent_files
from pydantic import BaseModel, ConfigDict, Field, ValidationError

EMAIL_ROOT: Final = Path(__file__).resolve().parents[2]
"""``examples/acme-email``: where ``hecklr.yaml`` and ``mailer.yaml`` live."""
MAILER_FILE: Final = Path("mailer.yaml")


class MailerSettings(BaseModel):
    """What ``mailer.yaml`` sets."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    thread_replies: bool = True
    """Reply in the customer's thread (``In-Reply-To``, ``References``). F11 turns it off."""
    tool_timeout_s: float | None = Field(default=5.0, gt=0)
    """Deadline of one world tool call; ``None`` waits forever, so a hung tool silences the
    reply (F12)."""


@dataclass(frozen=True, slots=True)
class EmailAgentFiles:
    """Everything the email agent runs from."""

    brain: AgentFiles
    mailer: MailerSettings
    variant: str
    """``clean`` or the overlay directory's name (e.g. ``F11``)."""


def load_email_agent_files(
    overlay: Path | None = None, *, email_root: Path = EMAIL_ROOT
) -> EmailAgentFiles:
    """Read the brain files and ``mailer.yaml``, the overlay replacing the one it holds.

    Raises:
        AgentConfigError: a file is missing or malformed, or the overlay holds anything but one
            replacement (and docs).
    """
    if overlay is None or not (overlay / MAILER_FILE).is_file():
        brain = load_agent_files(overlay=overlay)
        return EmailAgentFiles(brain, load_mailer(email_root / MAILER_FILE), brain.variant)
    _check_mailer_overlay(overlay)
    brain = load_agent_files()
    return EmailAgentFiles(brain, load_mailer(overlay / MAILER_FILE), overlay.name)


def load_mailer(path: Path) -> MailerSettings:
    """Parse one ``mailer.yaml``.

    Raises:
        AgentConfigError: the file cannot be read or does not hold valid settings.
    """
    try:
        document = yaml.safe_load(path.read_text(encoding="utf-8"))
        return MailerSettings.model_validate(document or {})
    except (OSError, yaml.YAMLError, ValidationError) as exc:
        raise AgentConfigError(f"cannot read the mailer settings {path}: {exc}") from exc


def _check_mailer_overlay(overlay: Path) -> None:
    files = {str(path.relative_to(overlay)) for path in overlay.rglob("*") if path.is_file()}
    others = files - {str(MAILER_FILE)} - OVERLAY_DOCS
    if others:
        editable = sorted(str(path) for path in EDITABLE)
        raise AgentConfigError(
            f"overlay {overlay} replaces {MAILER_FILE}, so it may not also hold {sorted(others)} "
            f"(a fault changes one file; brain files are {editable})"
        )
