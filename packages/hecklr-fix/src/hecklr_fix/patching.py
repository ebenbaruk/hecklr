"""Candidate patches: the model's find/replace edits, applied by Hecklr, shown as a unified diff.

Models write unreliable unified diffs (wrong line numbers, broken context), so the model proposes
edits as exact ``find`` -> ``replace`` pairs and Hecklr applies them and writes the diff with
``difflib``. The diff is therefore always exact and applies with ``git apply`` / ``patch -p1``.
"""

import difflib
from collections.abc import Mapping, Sequence
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from hecklr_fix.errors import PatchRejectedError


class ProposedEdit(BaseModel):
    """Replace one exact passage of one editable file."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    file: str
    """Path of the editable file, exactly as listed."""
    find: Annotated[str, Field(min_length=1)]
    """Text copied verbatim from the current file; it must occur exactly once."""
    replace: str
    """The new text for that passage."""


class ProposedPatch(BaseModel):
    """What the patch generator answers: why, and which edits."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    rationale: str
    """The root cause in the failing transcripts and how the edits address it."""
    edits: Annotated[list[ProposedEdit], Field(min_length=1)]


def apply_edits(files: Mapping[str, str], edits: Sequence[ProposedEdit]) -> dict[str, str]:
    """Apply ``edits`` in order to ``files`` and return the new text of every changed file.

    Args:
        files: The editable files (path -> current text); nothing else can be edited.
        edits: The edits; later edits see the result of earlier ones.

    Raises:
        PatchRejectedError: an edit names a file outside ``files``, its ``find`` text is missing
            or ambiguous, or the edits change nothing.
    """
    patched = dict(files)
    for number, edit in enumerate(edits, 1):
        if edit.file not in patched:
            editable = ", ".join(sorted(files))
            raise PatchRejectedError(
                f"edit {number} touches {edit.file!r}, which is not editable ({editable})"
            )
        text = patched[edit.file]
        occurrences = text.count(edit.find)
        if occurrences != 1:
            where = "not found in" if occurrences == 0 else f"found {occurrences} times in"
            raise PatchRejectedError(f"edit {number}: the text to replace is {where} {edit.file}")
        patched[edit.file] = text.replace(edit.find, edit.replace, 1)
    changed = {path: text for path, text in patched.items() if text != files[path]}
    if not changed:
        raise PatchRejectedError("the edits change nothing")
    return changed


def unified_diff(before: Mapping[str, str], after: Mapping[str, str]) -> str:
    """A ``git apply``-compatible unified diff of every file in ``after`` against ``before``."""
    parts: list[str] = []
    for path in sorted(after):
        parts.append(f"diff --git a/{path} b/{path}\n")
        lines = difflib.unified_diff(
            before[path].splitlines(keepends=True),
            after[path].splitlines(keepends=True),
            fromfile=f"a/{path}",
            tofile=f"b/{path}",
        )
        # difflib leaves a final line without its newline as is; diff and git mark it instead.
        parts.extend(
            line if line.endswith("\n") else f"{line}\n\\ No newline at end of file\n"
            for line in lines
        )
    return "".join(parts)
