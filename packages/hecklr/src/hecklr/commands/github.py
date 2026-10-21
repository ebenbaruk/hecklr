"""``hecklr github``: the GitHub Action's plumbing (hidden from ``--help``).

The composite action (``action/action.yml``) calls these through the same ``hecklr`` it installs,
so the logic is typed and tested here rather than written in shell:

- ``fetch-baseline``: download the newest baseline run artifact of a branch into the run store;
- ``latest-run``: the id of the newest finished run of a suite (to store it as the baseline);
- ``comment``: create or update the one sticky PR comment of a suite.

GitHub is reached only through the ``gh`` CLI (authenticated by ``GH_TOKEN``), which runners ship.
Each command writes its result to ``$GITHUB_OUTPUT`` when set, and to stdout.
"""

import json
import os
import shutil
import subprocess
import tempfile
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Annotated, Final

import typer

from hecklr import exit_codes
from hecklr.commands._common import (
    DEFAULT_CONFIG,
    ConfigPath,
    fail,
    load_project_or_exit,
    stderr,
)
from hecklr.results import FINISHED, runs_dir
from hecklr_core import ResultStore
from hecklr_core.storage import RUN_JSON
from hecklr_schemas import Run, RunStatus

Gh = Callable[[Sequence[str], str | None], str]
"""Runs ``gh`` with arguments (and optional stdin) and returns its stdout."""

COMMENT_MARKER: Final = "<!-- hecklr-gate:{suite} -->"
"""Hidden first line of the sticky comment; one comment per suite."""

app = typer.Typer(name="github", help="GitHub Action plumbing.", hidden=True, no_args_is_help=True)


def run_gh(args: Sequence[str], stdin: str | None = None) -> str:
    """``gh`` on PATH; a failure shows its stderr."""
    try:
        # Arguments are built by this module (ids, repo and artifact names), never a shell string.
        completed = subprocess.run(  # noqa: S603
            ["gh", *args],  # noqa: S607 -- gh is resolved on PATH like on every runner
            input=stdin,
            capture_output=True,
            text=True,
            check=True,
        )
    except FileNotFoundError as exc:
        raise GitHubError("the gh CLI is not installed") from exc
    except subprocess.CalledProcessError as exc:
        raise GitHubError(f"gh {' '.join(args[:2])} failed: {exc.stderr.strip()}") from exc
    return completed.stdout


class GitHubError(RuntimeError):
    """A GitHub API call through ``gh`` failed."""


@dataclass(frozen=True, slots=True)
class BaselineArtifact:
    """An uploaded baseline run: its artifact and the workflow run that uploaded it."""

    artifact_id: int
    workflow_run_id: int
    created_at: str


def find_baseline(gh: Gh, repo: str, name: str, branch: str) -> BaselineArtifact | None:
    """The newest unexpired artifact ``name`` uploaded by a workflow run on ``branch``."""
    endpoint = f"repos/{repo}/actions/artifacts?name={name}&per_page=100"
    listing = json.loads(gh(["api", endpoint], None))
    candidates = [
        BaselineArtifact(
            artifact_id=int(item["id"]),
            workflow_run_id=int(item["workflow_run"]["id"]),
            created_at=str(item["created_at"]),
        )
        for item in listing.get("artifacts", [])
        if not item.get("expired") and (item.get("workflow_run") or {}).get("head_branch") == branch
    ]
    return max(candidates, key=lambda a: (a.created_at, a.artifact_id), default=None)


def fetch_baseline(gh: Gh, repo: str, name: str, branch: str, runs: Path) -> Run | None:
    """Download the newest baseline artifact into ``runs/<run id>``; ``None`` if there is none."""
    artifact = find_baseline(gh, repo, name, branch)
    if artifact is None:
        return None
    with tempfile.TemporaryDirectory() as scratch:
        gh(
            [
                "run",
                "download",
                str(artifact.workflow_run_id),
                "-R",
                repo,
                "-n",
                name,
                "-D",
                scratch,
            ],
            None,
        )
        manifest = Path(scratch) / RUN_JSON
        if not manifest.is_file():
            raise GitHubError(
                f"artifact {name} of run {artifact.workflow_run_id} has no {RUN_JSON}"
            )
        run = Run.model_validate_json(manifest.read_text(encoding="utf-8"))
        target = runs / run.id
        if not target.exists():
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(scratch, target)
    return run


def upsert_comment(gh: Gh, repo: str, pr: int, marker: str, body: str) -> str:
    """Update the PR comment starting with ``marker``, or create it; returns what happened."""
    found = gh(
        [
            "api",
            "--paginate",
            f"repos/{repo}/issues/{pr}/comments",
            "--jq",
            f".[] | select(.body | startswith({json.dumps(marker)})) | .id",
        ],
        None,
    ).split()
    payload = json.dumps({"body": f"{marker}\n{body}"})
    if found:
        gh(
            ["api", "-X", "PATCH", f"repos/{repo}/issues/comments/{found[0]}", "--input", "-"],
            payload,
        )
        return "updated"
    gh(["api", "-X", "POST", f"repos/{repo}/issues/{pr}/comments", "--input", "-"], payload)
    return "created"


def latest_finished(runs: Path, suite: str) -> Run | None:
    """The newest finished run of ``suite`` under ``runs``."""
    return ResultStore(runs).latest_run(suite=suite, statuses=FINISHED)


def write_outputs(values: Mapping[str, str], env: Mapping[str, str] | None = None) -> None:
    """Append ``values`` to ``$GITHUB_OUTPUT`` (when set) and print them."""
    target = (os.environ if env is None else env).get("GITHUB_OUTPUT")
    lines = "".join(f"{key}={value}\n" for key, value in values.items())
    if target:
        with Path(target).open("a", encoding="utf-8") as outputs:
            outputs.write(lines)
    typer.echo(lines, nl=False)


def _gh(ctx: typer.Context) -> Gh:
    # Tests inject a fake through Click's context object, like CliOverrides for models.
    return ctx.obj if callable(ctx.obj) else run_gh


Repo = Annotated[str, typer.Option(envvar="GITHUB_REPOSITORY", help="owner/name.")]


@app.command("fetch-baseline")
def fetch_baseline_command(
    ctx: typer.Context,
    repo: Repo,
    name: Annotated[str, typer.Option(help="Baseline artifact name.")],
    branch: Annotated[str, typer.Option(help="Branch whose runs store the baseline.")],
    config: ConfigPath = DEFAULT_CONFIG,
) -> None:
    """Download the branch's newest baseline run into the run store; output ``run-id``."""
    project = load_project_or_exit(config)
    try:
        run = fetch_baseline(_gh(ctx), repo, name, branch, runs_dir(project))
    except GitHubError as exc:
        raise fail(str(exc), exit_codes.HARNESS_ERROR) from exc
    if run is None:
        stderr.print(f"No baseline artifact {name} from branch {branch} yet.")
    elif run.status is not RunStatus.COMPLETED:
        stderr.print(f"[yellow]The baseline run {run.id} is {run.status.value}.[/]")
    write_outputs({"run-id": run.id if run is not None else ""})


@app.command("latest-run")
def latest_run_command(
    suite: Annotated[str, typer.Option("--suite", "-s", help="Suite of the run.")],
    config: ConfigPath = DEFAULT_CONFIG,
) -> None:
    """Output ``run-id`` and ``status`` of the newest finished run of a suite."""
    project = load_project_or_exit(config)
    run = latest_finished(runs_dir(project), suite)
    if run is None:
        raise fail(f"no finished run of suite {suite!r}", exit_codes.HARNESS_ERROR)
    write_outputs({"run-id": run.id, "status": run.status.value})


@app.command("comment")
def comment_command(
    ctx: typer.Context,
    repo: Repo,
    pr: Annotated[int, typer.Option(help="Pull request number.")],
    suite: Annotated[str, typer.Option("--suite", "-s", help="Suite the comment is about.")],
    body_file: Annotated[
        Path | None, typer.Option("--body-file", help="Markdown body; missing means no report.")
    ] = None,
    fallback: Annotated[
        str, typer.Option(help="Body when the body file is missing (the gate never ran).")
    ] = "Hecklr could not produce a report; see the workflow logs.",
) -> None:
    """Create or update the suite's sticky PR comment; output ``comment`` (created/updated)."""
    body = (
        body_file.read_text(encoding="utf-8")
        if body_file is not None and body_file.is_file()
        else f"### Hecklr · HARNESS ERROR · suite `{suite}`\n\n{fallback}\n"
    )
    try:
        outcome = upsert_comment(_gh(ctx), repo, pr, COMMENT_MARKER.format(suite=suite), body)
    except GitHubError as exc:
        raise fail(str(exc), exit_codes.HARNESS_ERROR) from exc
    write_outputs({"comment": outcome})
