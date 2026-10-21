"""``hecklr github``: the Action's sticky comment and baseline artifacts, against a fake ``gh``."""

import json
import shutil
import subprocess
from collections.abc import Callable, Sequence
from pathlib import Path

import click
import pytest
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands.github import GitHubError, find_baseline, run_gh, write_outputs
from hecklr_schemas import Run, RunStatus

StoreRun = Callable[..., Run]
REPO = "acme/support-bot"
runner = CliRunner()


class FakeGh:
    """Answers the ``gh`` calls the Action makes, and records them."""

    def __init__(self, *, artifacts: list[dict[str, object]] | None = None) -> None:
        self.artifacts = artifacts or []
        self.comments: dict[int, str] = {}
        self.calls: list[list[str]] = []
        self.downloads: dict[str, Path] = {}
        """Workflow run id -> directory whose files the artifact holds."""

    def __call__(self, args: Sequence[str], stdin: str | None) -> str:
        self.calls.append(list(args))
        # Like gh itself: only the subcommands the Action uses exist.
        assert args[0] in {"api", "run"}, f"unknown command {args[0]!r} for gh"
        if args[1].startswith(f"repos/{REPO}/actions/artifacts"):
            return json.dumps({"artifacts": self.artifacts})
        if args[:2] == ["run", "download"]:
            shutil.copytree(self.downloads[args[2]], args[args.index("-D") + 1], dirs_exist_ok=True)
            return ""
        if "--paginate" in args:
            marker = json.loads(args[-1].split("startswith(")[1].split(")")[0])
            return "\n".join(str(i) for i, body in self.comments.items() if body.startswith(marker))
        assert stdin is not None
        body = json.loads(stdin)["body"]
        if args[2] == "PATCH":
            self.comments[int(args[3].rsplit("/", 1)[1])] = body
        else:
            self.comments[100 + len(self.comments)] = body
        return "{}"


def artifact(
    artifact_id: int, run_id: int, branch: str, created: str, **extra: object
) -> dict[str, object]:
    return {
        "id": artifact_id,
        "created_at": created,
        "expired": False,
        "workflow_run": {"id": run_id, "head_branch": branch},
        **extra,
    }


def invoke(gh: FakeGh, *args: str, env: dict[str, str] | None = None) -> tuple[int, str]:
    result = runner.invoke(app, ["github", *args], obj=gh, env=env)
    return result.exit_code, click.unstyle(result.output)


# --- baseline ---------------------------------------------------------------------------------


def test_the_newest_unexpired_artifact_of_the_branch_is_the_baseline() -> None:
    gh = FakeGh(
        artifacts=[
            artifact(1, 11, "main", "2026-10-20T10:00:00Z"),
            artifact(2, 12, "main", "2026-10-21T10:00:00Z"),
            artifact(3, 13, "feature", "2026-10-21T11:00:00Z"),
            artifact(4, 14, "main", "2026-10-21T12:00:00Z", expired=True),
        ]
    )

    found = find_baseline(gh, REPO, "hecklr-baseline-ci", "main")

    assert found is not None
    assert (found.artifact_id, found.workflow_run_id) == (2, 12)
    assert gh.calls[0] == [
        "api",
        f"repos/{REPO}/actions/artifacts?name=hecklr-baseline-ci&per_page=100",
    ]


def test_fetch_baseline_puts_the_run_into_the_store_and_outputs_its_id(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    uploaded = store({"s00": 1.0}, order=0)
    artifact_dir = tmp_path / "artifact"
    shutil.move(runs_dir / uploaded.id, artifact_dir)
    gh = FakeGh(artifacts=[artifact(2, 12, "main", "2026-10-21T10:00:00Z")])
    gh.downloads["12"] = artifact_dir
    outputs = tmp_path / "github_output"

    code, output = invoke(
        gh,
        "fetch-baseline", "--repo", REPO, "--name", "hecklr-baseline-ci", "--branch", "main",
        "--config", str(config_path),
        env={"GITHUB_OUTPUT": str(outputs)},
    )  # fmt: skip

    assert code == exit_codes.OK, output
    assert (runs_dir / uploaded.id / "run.json").is_file()
    assert outputs.read_text("utf-8") == f"run-id={uploaded.id}\n"
    assert gh.calls[1][:7] == ["run", "download", "12", "-R", REPO, "-n", "hecklr-baseline-ci"]


def test_fetch_baseline_without_any_artifact_outputs_nothing(config_path: Path) -> None:
    code, output = invoke(
        FakeGh(), "fetch-baseline", "--repo", REPO, "--name", "n", "--branch", "main",
        "--config", str(config_path),
    )  # fmt: skip

    assert code == exit_codes.OK
    assert "No baseline artifact n from branch main yet." in output
    assert "run-id=\n" in output


def test_a_baseline_artifact_without_a_run_is_a_harness_error(
    config_path: Path, tmp_path: Path
) -> None:
    gh = FakeGh(artifacts=[artifact(2, 12, "main", "2026-10-21T10:00:00Z")])
    gh.downloads["12"] = tmp_path  # no run.json in it

    code, output = invoke(
        gh, "fetch-baseline", "--repo", REPO, "--name", "n", "--branch", "main",
        "--config", str(config_path),
    )  # fmt: skip

    assert code == exit_codes.HARNESS_ERROR
    assert "has no run.json" in output


def test_a_failed_baseline_run_is_fetched_with_a_warning(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    uploaded = store({"s00": 1.0}, status=RunStatus.FAILED)
    artifact_dir = tmp_path / "artifact"
    shutil.move(runs_dir / uploaded.id, artifact_dir)
    gh = FakeGh(artifacts=[artifact(2, 12, "main", "2026-10-21T10:00:00Z")])
    gh.downloads["12"] = artifact_dir

    code, output = invoke(
        gh, "fetch-baseline", "--repo", REPO, "--name", "n", "--branch", "main",
        "--config", str(config_path),
    )  # fmt: skip

    assert code == exit_codes.OK
    assert f"The baseline run {uploaded.id} is failed." in output


# --- latest run -------------------------------------------------------------------------------


def test_latest_run_outputs_the_newest_finished_run_of_the_suite(
    config_path: Path, store: StoreRun
) -> None:
    store({"s00": 1.0}, order=0)
    newest = store({"s00": 1.0}, order=1, status=RunStatus.BUDGET_EXCEEDED)
    store({"s00": 1.0}, order=2, suite="nightly")

    code, output = invoke(FakeGh(), "latest-run", "--suite", "ci", "--config", str(config_path))

    assert code == exit_codes.OK
    assert output == f"run-id={newest.id}\nstatus=budget_exceeded\n"


def test_latest_run_without_runs_is_a_harness_error(config_path: Path) -> None:
    code, output = invoke(FakeGh(), "latest-run", "--suite", "ci", "--config", str(config_path))

    assert code == exit_codes.HARNESS_ERROR
    assert "no finished run of suite 'ci'" in output


def test_latest_run_since_keeps_only_runs_this_job_made(config_path: Path, store: StoreRun) -> None:
    store({"s00": 1.0}, order=0)  # 09:00, the fetched baseline
    made = store({"s00": 1.0}, order=5)  # 09:05

    code, output = invoke(
        FakeGh(), "latest-run", "--suite", "ci", "--since", "2026-10-21T09:03:00Z",
        "--config", str(config_path),
    )  # fmt: skip

    assert code == exit_codes.OK, output
    assert output == f"run-id={made.id}\nstatus=completed\n"


@pytest.mark.parametrize("since", ["yesterday", "2026-10-21T09:03:00"])
def test_latest_run_since_needs_an_iso_time_with_a_zone(config_path: Path, since: str) -> None:
    code, output = invoke(
        FakeGh(), "latest-run", "--suite", "ci", "--since", since, "--config", str(config_path)
    )

    assert code == 2
    assert "--since" in output


def test_us04_a_run_that_recorded_nothing_is_never_gated_against_the_fetched_baseline(
    config_path: Path, store: StoreRun, runs_dir: Path, tmp_path: Path
) -> None:
    """The Action's steps after `hecklr run` exits before recording (e.g. a missing API key)."""
    uploaded = store({f"s{i:02d}": 1.0 for i in range(10)}, suite="ci", order=0)
    artifact_dir = tmp_path / "artifact"
    shutil.move(runs_dir / uploaded.id, artifact_dir)
    gh = FakeGh(artifacts=[artifact(2, 12, "main", "2026-10-21T10:00:00Z")])
    gh.downloads["12"] = artifact_dir
    code, output = invoke(
        gh, "fetch-baseline", "--repo", REPO, "--name", "hecklr-baseline-ci", "--branch", "main",
        "--config", str(config_path),
    )  # fmt: skip
    assert code == exit_codes.OK, output

    # The run step started after the baseline was made, then failed without recording a run.
    code, output = invoke(
        gh, "latest-run", "--suite", "ci", "--since", "2026-10-21T12:00:00+00:00",
        "--config", str(config_path),
    )  # fmt: skip
    assert code == exit_codes.HARNESS_ERROR
    assert "run-id=" not in output

    # Even gated by hand, the fetched baseline cannot stand in for the candidate.
    result = runner.invoke(
        app, ["gate", "--suite", "ci", "--baseline", uploaded.id, "--config", str(config_path)]
    )
    assert result.exit_code == exit_codes.HARNESS_ERROR, result.output
    assert "same run" in click.unstyle(result.output)


# --- sticky comment ---------------------------------------------------------------------------


def test_us05_one_sticky_comment_per_suite_is_created_then_updated(tmp_path: Path) -> None:
    gh = FakeGh()
    gh.comments[7] = "an unrelated review comment"
    body = tmp_path / "report.md"
    body.write_text("### Hecklr · REGRESSION\n", encoding="utf-8")

    code, output = invoke(
        gh, "comment", "--repo", REPO, "--pr", "5", "-s", "ci", "--body-file", str(body)
    )
    assert code == exit_codes.OK, output
    assert output == "comment=created\n"

    body.write_text("### Hecklr · NO CHANGE\n", encoding="utf-8")
    code, output = invoke(
        gh, "comment", "--repo", REPO, "--pr", "5", "-s", "ci", "--body-file", str(body)
    )
    assert output == "comment=updated\n"

    code, _ = invoke(
        gh, "comment", "--repo", REPO, "--pr", "5", "-s", "nightly", "--body-file", str(body)
    )
    gate_comments = [c for c in gh.comments.values() if c.startswith("<!-- hecklr-gate:")]
    assert len(gate_comments) == 2
    assert "<!-- hecklr-gate:ci -->\n### Hecklr · NO CHANGE\n" in gate_comments
    assert gh.comments[7] == "an unrelated review comment"


def test_comment_without_a_report_says_the_harness_failed(tmp_path: Path) -> None:
    gh = FakeGh()

    code, _ = invoke(
        gh, "comment", "--repo", REPO, "--pr", "5", "-s", "ci",
        "--body-file", str(tmp_path / "missing.md"), "--fallback", "hecklr run exited 78.",
    )  # fmt: skip

    assert code == exit_codes.OK
    (comment,) = gh.comments.values()
    assert "HARNESS ERROR" in comment
    assert "hecklr run exited 78." in comment


def test_the_repository_comes_from_the_actions_environment(tmp_path: Path) -> None:
    gh = FakeGh()

    code, output = invoke(gh, "comment", "--pr", "5", "-s", "ci", env={"GITHUB_REPOSITORY": REPO})

    assert code == exit_codes.OK, output
    assert gh.calls[0][2] == f"repos/{REPO}/issues/5/comments"


def test_a_failing_gh_call_is_a_harness_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def broken(args: Sequence[str], stdin: str | None) -> str:
        raise GitHubError("gh api failed: HTTP 403")

    result = runner.invoke(
        app, ["github", "comment", "--repo", REPO, "--pr", "5", "-s", "ci"], obj=broken
    )

    assert result.exit_code == exit_codes.HARNESS_ERROR
    assert "HTTP 403" in click.unstyle(result.output)


# --- gh itself --------------------------------------------------------------------------------


def test_run_gh_returns_stdout_and_explains_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(argv: list[str], **kwargs: object) -> subprocess.CompletedProcess[str]:
        if argv[1] == "fail":
            raise subprocess.CalledProcessError(1, argv, stderr="HTTP 404: Not Found\n")
        if argv[1] == "missing":
            raise FileNotFoundError("gh")
        return subprocess.CompletedProcess(argv, 0, stdout=f"ok {kwargs['input']}")

    monkeypatch.setattr(subprocess, "run", fake_run)

    assert run_gh(["api", "x"], "in") == "ok in"
    with pytest.raises(GitHubError, match="gh fail x failed: HTTP 404: Not Found"):
        run_gh(["fail", "x"])
    with pytest.raises(GitHubError, match="not installed"):
        run_gh(["missing"])


def test_outputs_are_printed_without_github_output(capsys: pytest.CaptureFixture[str]) -> None:
    write_outputs({"a": "1"}, env={})

    assert capsys.readouterr().out == "a=1\n"
