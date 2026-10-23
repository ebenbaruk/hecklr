"""``--pr`` against a real local repository, a bare ``origin`` and a recording fake ``gh``."""

import os
import shutil
import subprocess
from collections.abc import Sequence
from pathlib import Path

import pytest

from hecklr_fix.errors import FixError
from hecklr_fix.pr import open_pull_request, run_command

pytestmark = pytest.mark.skipif(shutil.which("git") is None, reason="needs git")

FAKE_GH = """#!/bin/sh
printf '%s\\n' "$@" > "$GH_ARGS"
for a in "$@"; do if [ "$prev" = "--body-file" ]; then cp "$a" "$GH_ARGS.body"; fi; prev="$a"; done
echo "Creating pull request"
echo "https://github.test/acme/agent/pull/7"
"""


def git(cwd: Path, *args: str) -> str:
    return run_command(["git", *args], cwd)


@pytest.fixture
def repo(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """A repository whose agent project lives in ``agent/``, pushed to a bare origin."""
    origin = tmp_path / "origin.git"
    subprocess.run(["git", "init", "-q", "--bare", str(origin)], check=True)  # noqa: S603, S607
    root = tmp_path / "repo"
    (root / "agent" / "prompts").mkdir(parents=True)
    (root / "agent" / "prompts" / "system.md").write_text("Book the first free slot.\n")
    git(tmp_path, "init", "-q", "-b", "main", str(root))
    for key, value in (("user.name", "Test"), ("user.email", "test@example.test")):
        git(root, "config", key, value)
    git(root, "remote", "add", "origin", str(origin))
    git(root, "add", ".")
    git(root, "commit", "-q", "-m", "initial")
    git(root, "push", "-q", "-u", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH)
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_ARGS", str(tmp_path / "gh-args"))
    return root


def test_us10_pr_commits_the_patch_on_a_branch_and_opens_a_pull_request(
    repo: Path, tmp_path: Path
) -> None:
    pr = open_pull_request(
        repo / "agent",
        {"prompts/system.md": "Confirm the slot first.\n"},
        branch="hecklr-fix/abc",
        title="fix(agent): confirm slots before booking",
        body="## report\n",
        draft=True,
    )

    assert pr.url == "https://github.test/acme/agent/pull/7"
    assert (pr.branch, pr.base) == ("hecklr-fix/abc", "main")
    shown = git(repo, "show", "origin/hecklr-fix/abc:agent/prompts/system.md")
    assert shown == "Confirm the slot first.\n"
    assert git(repo, "log", "-1", "--format=%s", "origin/hecklr-fix/abc").strip() == (
        "fix(agent): confirm slots before booking"
    )
    args = (tmp_path / "gh-args").read_text().splitlines()
    assert args[:10] == [
        "pr",
        "create",
        "--base",
        "main",
        "--head",
        "hecklr-fix/abc",
        "--title",
        "fix(agent): confirm slots before booking",
        "--body-file",
        args[9],
    ]
    assert args[10:] == ["--draft"]
    assert (tmp_path / "gh-args.body").read_text() == "## report\n"
    # The user's checkout is untouched and no worktree is left behind.
    assert (repo / "agent" / "prompts" / "system.md").read_text() == "Book the first free slot.\n"
    assert git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip() == "main"
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_uncommitted_edits_to_the_patched_files_are_refused(repo: Path) -> None:
    (repo / "agent" / "prompts" / "system.md").write_text("local edit\n")

    with pytest.raises(FixError, match="commit or stash your changes"):
        open_pull_request(
            repo / "agent", {"prompts/system.md": "x\n"}, branch="b", title="t", body=""
        )


def test_a_failing_command_is_a_fix_error_and_cleans_up(repo: Path) -> None:
    calls: list[Sequence[str]] = []

    def failing_push(argv: Sequence[str], cwd: Path) -> str:
        calls.append(argv)
        if list(argv[:2]) == ["git", "push"]:
            raise FixError("git push failed (1): denied")
        return run_command(argv, cwd)

    with pytest.raises(FixError, match="git push failed"):
        open_pull_request(
            repo / "agent",
            {"prompts/system.md": "x\n"},
            branch="b",
            title="t",
            body="",
            run=failing_push,
        )

    assert calls[-1][:3] == ["git", "worktree", "remove"]
    assert len(git(repo, "worktree", "list").splitlines()) == 1


def test_gh_without_output_and_missing_tools_fail_loudly(repo: Path) -> None:
    def silent_gh(argv: Sequence[str], cwd: Path) -> str:
        return "" if argv[0] == "gh" else run_command(argv, cwd)

    with pytest.raises(FixError, match="printed no pull request URL"):
        open_pull_request(
            repo / "agent",
            {"prompts/system.md": "y\n"},
            branch="c",
            title="t",
            body="",
            run=silent_gh,
        )
    with pytest.raises(FixError, match="cannot run no-such-tool"):
        run_command(["no-such-tool"], repo)
