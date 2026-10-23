"""The files the fixer may change: ``agent.editable`` in ``hecklr.yaml``, and nothing else.

Guardrail (spec, "Fixer"): the fixer never edits scenarios, graders, rubrics or files outside
``editable``. Rubrics and graders ship with ``hecklr-grade``, outside the project. Inside it, the
files that define what is tested (``hecklr.yaml``, the world, every scenario and persona a suite
can select) are refused even when ``editable`` lists them, so a patch can never pass by changing
the test instead of the agent.
"""

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path, PurePosixPath

from hecklr_core import PERSONA_LIBRARY_GLOB
from hecklr_fix.errors import FixError
from hecklr_schemas import HecklrConfig


@dataclass(frozen=True, slots=True)
class EditableFiles:
    """The current text of every editable file, keyed by its path relative to the project."""

    root: Path
    """Directory holding ``hecklr.yaml``."""
    contents: Mapping[str, str]
    """POSIX path relative to ``root`` -> file text."""


def load_editable(config: HecklrConfig, config_path: Path) -> EditableFiles:
    """Read the files ``agent.editable`` declares, after checking each may be edited.

    Raises:
        FixError: nothing is editable, or an entry is absolute, leaves the project, is missing,
            is not UTF-8 text, or defines the tests (config, world, scenarios, personas).
    """
    root = config_path.resolve().parent
    if not config.agent.editable:
        raise FixError(
            "hecklr.yaml declares no agent.editable files: list the prompt and tool description "
            "files the fixer may change"
        )
    protected = protected_files(config, config_path)
    contents: dict[str, str] = {}
    for entry in config.agent.editable:
        relative = normalize(entry)
        path = (root / relative).resolve()
        if not path.is_relative_to(root):
            raise FixError(f"editable file {entry!r} is outside the project {root}")
        if path in protected:
            raise FixError(
                f"editable file {entry!r} defines the tests (hecklr.yaml, world, scenario or "
                "persona); the fixer never edits those"
            )
        try:
            contents[relative] = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            raise FixError(f"cannot read editable file {entry!r}: {exc}") from exc
    return EditableFiles(root=root, contents=contents)


def normalize(entry: str) -> str:
    """``entry`` as a clean relative POSIX path.

    Raises:
        FixError: ``entry`` is empty, absolute or climbs out with ``..``.
    """
    path = PurePosixPath(entry.replace("\\", "/"))
    if not entry.strip() or path.is_absolute() or ".." in path.parts:
        raise FixError(f"editable file {entry!r} must be a relative path inside the project")
    return path.as_posix()


def protected_files(config: HecklrConfig, config_path: Path) -> frozenset[Path]:
    """Resolved paths of every file that defines what the suites test."""
    root = config_path.resolve().parent
    patterns = [PERSONA_LIBRARY_GLOB]
    for suite in config.suites.values():
        patterns.append(suite.scenarios)
        if suite.personas is not None:
            patterns.append(suite.personas)
    globbed = {path.resolve() for pattern in patterns for path in root.glob(pattern)}
    return frozenset({config_path.resolve(), (root / config.world).resolve(), *globbed})
