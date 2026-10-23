import shutil
import subprocess
from pathlib import Path

import pytest
from hypothesis import given
from hypothesis import strategies as st

from hecklr_fix.errors import PatchRejectedError
from hecklr_fix.patching import ProposedEdit, apply_edits, unified_diff

PROMPT = "Rules:\n- Book the first free slot.\n- Verify before refunds.\n"
FILES = {"prompts/system.md": PROMPT, "tools/descriptions.yaml": "tools: {}\n"}


def _edit(find: str, replace: str, file: str = "prompts/system.md") -> ProposedEdit:
    return ProposedEdit(file=file, find=find, replace=replace)


def test_edits_apply_in_order_and_only_changed_files_are_returned() -> None:
    changed = apply_edits(
        FILES,
        [
            _edit("Book the first free slot.", "Offer slots and confirm one."),
            _edit("confirm one.", "confirm the exact slot."),
        ],
    )

    assert changed == {
        "prompts/system.md": (
            "Rules:\n- Offer slots and confirm the exact slot.\n- Verify before refunds.\n"
        )
    }


def test_guardrail_an_edit_outside_the_editable_files_is_rejected() -> None:
    with pytest.raises(PatchRejectedError, match=r"touches 'scenarios/a.yaml', which is not"):
        apply_edits(FILES, [_edit("x", "y", file="scenarios/a.yaml")])


@pytest.mark.parametrize(
    ("find", "message"),
    [("not there", "not found in"), ("\n-", "found 2 times in")],
)
def test_missing_or_ambiguous_find_text_is_rejected(find: str, message: str) -> None:
    with pytest.raises(PatchRejectedError, match=message):
        apply_edits(FILES, [_edit(find, "z")])


def test_a_patch_that_changes_nothing_is_rejected() -> None:
    with pytest.raises(PatchRejectedError, match="change nothing"):
        apply_edits(FILES, [_edit("Rules:", "Rules:")])


def test_the_diff_names_each_changed_file_and_marks_a_missing_final_newline() -> None:
    before = {"a.md": "one\ntwo", "b.md": "x\n"}
    after = {"a.md": "one\nthree", "b.md": "y\n"}

    diff = unified_diff(before, after)

    assert diff.index("diff --git a/a.md b/a.md") < diff.index("diff --git a/b.md b/b.md")
    assert "-two\n\\ No newline at end of file\n+three\n\\ No newline at end of file\n" in diff
    assert "-x\n+y\n" in diff


@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
@pytest.mark.parametrize(
    ("old", "new"),
    [(PROMPT, PROMPT.replace("first free", "confirmed")), ("a\nb", "a\nb\nc"), ("a\n", "a")],
)
def test_the_diff_applies_with_git_apply(tmp_path: Path, old: str, new: str) -> None:
    (tmp_path / "prompts").mkdir()
    target = tmp_path / "prompts" / "system.md"
    target.write_text(old, encoding="utf-8")
    patch = tmp_path / "fix.diff"
    patch.write_text(unified_diff({"prompts/system.md": old}, {"prompts/system.md": new}), "utf-8")

    for args in (["init", "-q", str(tmp_path)], ["-C", str(tmp_path), "apply", str(patch)]):
        subprocess.run(["git", *args], check=True)  # noqa: S603, S607 -- git on PATH, fixed args

    assert target.read_text(encoding="utf-8") == new


@given(
    st.lists(st.text(alphabet="ab\n", max_size=6), min_size=1, max_size=4),
    st.text(alphabet="xyz", min_size=1, max_size=3),
)
def test_property_a_unique_find_is_replaced_exactly_once(pieces: list[str], marker: str) -> None:
    text = "".join(pieces) + marker + "".join(reversed(pieces))
    changed = apply_edits({"f": text}, [ProposedEdit(file="f", find=marker, replace=marker + "!")])

    assert changed["f"] == "".join(pieces) + marker + "!" + "".join(reversed(pieces))
