"""``hecklr fix --pr``: open a pull request with the winning patch through ``git`` and ``gh``.

The user's checkout is never touched: the patch is committed on a new branch in a temporary
``git worktree`` from ``HEAD``, pushed, and proposed with ``gh pr create``. The edited files must
match ``HEAD`` (the patch was computed against them), so uncommitted edits to them are refused.
A human reviews and merges the pull request; Hecklr never merges.
"""

import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from hecklr_fix.errors import FixError

CommandRunner = Callable[[Sequence[str], Path], str]
"""Runs a command in a directory and returns its stdout; raises ``FixError`` when it fails."""


def run_command(argv: Sequence[str], cwd: Path) -> str:
    """The default ``CommandRunner``: no shell, output captured, failures raised with stderr."""
    try:
        done = subprocess.run(  # noqa: S603 -- git and gh with arguments Hecklr builds, no shell
            list(argv), cwd=cwd, capture_output=True, text=True, check=False
        )
    except OSError as exc:
        raise FixError(f"cannot run {argv[0]}: {exc}") from exc
    if done.returncode != 0:
        raise FixError(f"{' '.join(argv[:3])} failed ({done.returncode}): {done.stderr.strip()}")
    return done.stdout


@dataclass(frozen=True, slots=True)
class PullRequest:
    """What ``--pr`` opened."""

    url: str
    branch: str
    base: str


def open_pull_request(
    project_root: Path,
    files: Mapping[str, str],
    *,
    branch: str,
    title: str,
    body: str,
    draft: bool = False,
    run: CommandRunner = run_command,
) -> PullRequest:
    """Commit ``files`` (project-relative path -> new text) on ``branch`` and open a PR.

    Raises:
        FixError: not a git repository, an edited file has uncommitted changes, or a git or gh
            command failed.
    """
    top = Path(run(["git", "rev-parse", "--show-toplevel"], project_root).strip())
    prefix = project_root.resolve().relative_to(top.resolve())
    paths = [str(prefix / relative) for relative in sorted(files)]
    if run(["git", "status", "--porcelain", "--", *paths], top).strip():
        raise FixError(
            f"commit or stash your changes to {', '.join(paths)} first: the patch was computed "
            "against the working tree and the pull request starts from HEAD"
        )
    base = run(["git", "rev-parse", "--abbrev-ref", "HEAD"], top).strip()
    with tempfile.TemporaryDirectory(prefix="hecklr-fix-pr-") as scratch:
        worktree = Path(scratch) / "worktree"
        run(["git", "worktree", "add", "-q", "-b", branch, str(worktree), "HEAD"], top)
        try:
            for path, relative in zip(paths, sorted(files), strict=True):
                (worktree / path).write_text(files[relative], encoding="utf-8")
            run(["git", "add", "--", *paths], worktree)
            run(["git", "commit", "-q", "-m", title, "-m", "Proposed by hecklr fix."], worktree)
            run(["git", "push", "-q", "-u", "origin", branch], worktree)
            body_file = Path(scratch) / "body.md"
            body_file.write_text(body, encoding="utf-8")
            command = ["gh", "pr", "create", "--base", base, "--head", branch, "--title", title]
            command += ["--body-file", str(body_file), *(["--draft"] if draft else [])]
            output = run(command, worktree).strip()
            if not output:
                raise FixError("gh pr create printed no pull request URL")
            url = output.splitlines()[-1]
        finally:
            run(["git", "worktree", "remove", "--force", str(worktree)], top)
    return PullRequest(url=url, branch=branch, base=base)
