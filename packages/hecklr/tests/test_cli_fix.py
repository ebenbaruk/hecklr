"""``hecklr fix`` end to end on the reference agent: real agent processes, real runs, no model.

The acme-maintenance rules brain reads its system prompt (``acme_agent/rules/behaviour.py``), so
a prompt patch changes its behaviour exactly as it changes what the LLM brain is told; with the
scripted ``smoke`` suite every run is deterministic and free. ``FakeLLM`` plays the patch
generator, proposing the fix for F1, a planted patch that also breaks refunds (F5's rule) and an
irrelevant one.
"""

import os
import re
import shutil
import socket
import subprocess
import sys
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

import click
import pytest
import yaml
from typer.testing import CliRunner

from hecklr import exit_codes
from hecklr.cli import app
from hecklr.commands._common import CliOverrides
from hecklr_core import load_model
from hecklr_fix import FixSummary, ProposedEdit, ProposedPatch, free_port
from hecklr_llm import FakeLLM
from hecklr_schemas import (
    ClusterId,
    FailureCluster,
    Patch,
    PatchStatus,
    ProposalId,
    Scenario,
    ScenarioProposal,
    new_id,
)

# The agents and the world sandbox listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

EXAMPLES = Path(__file__).resolve().parents[3] / "examples"
ACME = EXAMPLES / "acme-maintenance"
AGENT_CMD = "{python} agent/serve.py --brain rules --port {port}"
runner = CliRunner()


def _changed_line(fault: str) -> tuple[str, str]:
    """(clean line, faulty line) of a fault overlay's one-line prompt change."""
    clean = (ACME / "prompts" / "system.md").read_text().splitlines()
    faulty = (EXAMPLES / "acme-faults" / fault / "prompts" / "system.md").read_text().splitlines()
    ((before, after),) = [(a, b) for a, b in zip(clean, faulty, strict=True) if a != b]
    return before, after


CONFIRM_RULE, F1_RULE = _changed_line("F1")
REFUND_RULE, F5_RULE = _changed_line("F5")


def _proposal(rationale: str, *edits: tuple[str, str]) -> ProposedPatch:
    return ProposedPatch(
        rationale=rationale,
        edits=[ProposedEdit(file="prompts/system.md", find=f, replace=r) for f, r in edits],
    )


RESTORE_CONFIRMATION = _proposal("The prompt books without confirming.", (F1_RULE, CONFIRM_RULE))
PLANTED_BAD = _proposal(
    "Confirm slots; also speed up refunds.", (F1_RULE, CONFIRM_RULE), (REFUND_RULE, F5_RULE)
)
IRRELEVANT = _proposal("Be friendlier.", ("Be brief, friendly", "Be brief, warm"))


def _copy_project(dest: Path, *, fault: str | None) -> Path:
    shutil.copytree(
        ACME, dest, ignore=shutil.ignore_patterns(".hecklr", "__pycache__", "tests", "redteam")
    )
    if fault is not None:
        overlay = EXAMPLES / "acme-faults" / fault / "prompts" / "system.md"
        shutil.copy(overlay, dest / "prompts" / "system.md")
    return dest / "hecklr.yaml"


@contextmanager
def _agent(project: Path) -> Iterator[None]:
    """The project's own agent on a free port, with ``hecklr.yaml`` pointed at it."""
    port = free_port()
    config = yaml.safe_load(project.read_text())
    chat = config["agent"]["adapters"]["chat"]
    chat["url"] = re.sub(r":\d+/", f":{port}/", chat["url"])
    project.write_text(yaml.safe_dump(config, sort_keys=False))
    command = AGENT_CMD.format(python=sys.executable, port=port).split()
    process = subprocess.Popen(command, cwd=project.parent)  # noqa: S603 -- our interpreter and script
    try:
        _wait_for(port)
        yield
    finally:
        process.terminate()
        process.wait(timeout=10)


def _wait_for(port: int) -> None:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        with socket.socket() as probe:
            if probe.connect_ex(("127.0.0.1", port)) == 0:
                return
        time.sleep(0.1)
    pytest.fail(f"the agent did not listen on {port}")


def _invoke(args: list[str], llm: FakeLLM | None = None) -> tuple[int, str]:
    providers = {"fake": llm} if llm is not None else {}
    result = runner.invoke(app, args, obj=CliOverrides(providers))
    return result.exit_code, click.unstyle(result.output)


def _git(cwd: Path, *args: str) -> str:
    done = subprocess.run(  # noqa: S603 -- fixed git arguments
        ["git", *args],  # noqa: S607 -- git on PATH
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout


def _repository(root: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """Make ``root`` a repository with a bare origin; put a recording fake ``gh`` on PATH."""
    origin = tmp_path / "origin.git"
    _git(tmp_path, "init", "-q", "--bare", str(origin))
    _git(root, "init", "-q", "-b", "main")
    _git(root, "config", "user.name", "Test")
    _git(root, "config", "user.email", "test@example.test")
    _git(root, "add", ".")
    _git(root, "commit", "-q", "-m", "agent with the F1 prompt")
    _git(root, "remote", "add", "origin", str(origin))
    _git(root, "push", "-q", "-u", "origin", "main")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text('#!/bin/sh\nprintf "%s\\n" "$@" > "$GH_ARGS"\necho https://github.test/pr/1\n')
    gh.chmod(0o755)
    monkeypatch.setenv("PATH", f"{bin_dir}{os.pathsep}{os.environ['PATH']}")
    monkeypatch.setenv("GH_ARGS", str(tmp_path / "gh-args"))
    return origin


@pytest.mark.slow
@pytest.mark.e2e
@pytest.mark.skipif(shutil.which("git") is None, reason="needs git")
def test_ws15_fixes_f1_and_the_guard_rejects_a_planted_bad_patch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    project = _copy_project(tmp_path / "acme", fault="F1")
    with _agent(project):
        code, output = _invoke(["run", "--config", str(project), "--suite", "smoke"])
    assert code == exit_codes.OK, output
    run_id = re.search(r"Run (\S+) \(completed\)", output)
    assert run_id, output
    origin = _repository(project.parent, tmp_path, monkeypatch)
    generator = FakeLLM(script=[RESTORE_CONFIRMATION, PLANTED_BAD, IRRELEVANT])

    code, output = _invoke(
        [
            "fix", "--config", str(project), "--from-run", run_id.group(1),
            "--tag", "catches-f1", "--agent-cmd", AGENT_CMD, "--model", "fake/fixer",
            "--candidates", "3", "--trials", "3",
            "--guard-suite", "smoke", "--guard-trials", "3", "--pr", "--draft",
            "--budget-usd", "0",  # #118: free models (rules brain, fake/fixer) run on $0
        ],
        generator,
    )  # fmt: skip

    assert code == exit_codes.OK, output
    assert "# hecklr fix: a validated patch is ready for review" in output
    session = Path(re.search(r"Session: (\S+)", output).group(1))  # type: ignore[union-attr]
    summary = FixSummary.model_validate_json((session / "summary.json").read_text())
    assert (summary.outcome.value, summary.winner_candidate) == ("fixed", 0)
    patches = [
        Patch.model_validate_json(line)
        for line in (session / "patches.jsonl").read_text().splitlines()
    ]
    assert [p.status for p in patches] == [
        PatchStatus.ACCEPTED,
        PatchStatus.REJECTED_GUARD,
        PatchStatus.REJECTED_TARGETED,
    ]
    fixed, planted, _ = patches
    assert fixed.targeted[0].verdict.value == "improvement"
    assert fixed.targeted[0].n_scenarios == 27  # 9 catches-f1 scenarios x 3 trials, as cells
    assert not [c for c in fixed.guard if c.verdict.value == "regression"]
    regressed = {c.slice.get("tag") for c in planted.guard if c.verdict.value == "regression"}
    assert "catches-f5" in regressed
    # The winning diff turns the F1 prompt back into the clean one.
    prompt = project.parent / "prompts" / "system.md"
    assert F1_RULE in prompt.read_text()
    subprocess.run(  # noqa: S603 -- patch(1) with fixed arguments
        ["patch", "-s", "-p1", "-d", str(project.parent), "-i", str(session / "winner.diff")],  # noqa: S607
        check=True,
    )
    assert prompt.read_text() == (ACME / "prompts" / "system.md").read_text()
    # --pr: a draft pull request from a branch holding exactly the winning patch.
    branch = f"hecklr-fix/{summary.fix_id.lower()}"
    assert "Pull request: https://github.test/pr/1" in output
    pushed = _git(project.parent, "show", f"origin/{branch}:prompts/system.md")
    assert pushed == (ACME / "prompts" / "system.md").read_text()
    gh_args = (tmp_path / "gh-args").read_text().splitlines()
    assert gh_args[:6] == ["pr", "create", "--base", "main", "--head", branch]
    assert gh_args[-1] == "--draft"
    assert origin.is_dir()


@pytest.mark.slow
@pytest.mark.e2e
def test_issue113_fix_never_writes_through_a_symlinked_prompt_directory(tmp_path: Path) -> None:
    # The red team's repro: prompts/ is an absolute link to a directory of the project itself,
    # which `load_editable` accepts; the rejected candidate's text used to land in that file.
    project = _copy_project(tmp_path / "acme", fault="F1")
    real = project.parent / "agent_prompts"
    (project.parent / "prompts").rename(real)
    (project.parent / "prompts").symlink_to(real.resolve(), target_is_directory=True)
    original = (real / "system.md").read_text()
    with _agent(project):
        code, output = _invoke(["run", "--config", str(project), "--suite", "smoke"])
    assert code == exit_codes.OK, output
    run_id = re.search(r"Run (\S+) \(completed\)", output)
    assert run_id, output

    code, output = _invoke(
        [
            "fix", "--config", str(project), "--from-run", run_id.group(1),
            "--tag", "catches-f1", "--agent-cmd", AGENT_CMD, "--model", "fake/fixer",
            "--candidates", "1", "--trials", "1",
            "--guard-suite", "smoke", "--guard-trials", "1", "--budget-usd", "0.01",
        ],
        FakeLLM(script=[IRRELEVANT]),
    )  # fmt: skip

    assert "rejected: no significant improvement" in output, output
    assert (real / "system.md").read_text() == original


@pytest.mark.slow
@pytest.mark.e2e
def test_us10_a_mined_cluster_the_agent_already_handles_is_reported_not_patched(
    tmp_path: Path,
) -> None:
    project = _copy_project(tmp_path / "acme", fault=None)
    scenario = load_model(ACME / "scripted" / "scripted-booking-1.yaml", Scenario)
    now = datetime.now(UTC)
    cluster = FailureCluster(
        id=ClusterId(new_id()),
        label="books the wrong slot",
        source="production",
        conversation_ids=["c-1"],
        created_at=now,
    )
    proposal = ScenarioProposal(
        id=ProposalId(new_id()), cluster_id=cluster.id, scenario=scenario, created_at=now
    )
    mine = project.parent / ".hecklr" / "mine" / new_id()
    mine.mkdir(parents=True)
    (mine / "clusters.jsonl").write_text(cluster.model_dump_json() + "\n")
    (mine / "proposals.jsonl").write_text(proposal.model_dump_json() + "\n")
    generator = FakeLLM(script=[])

    code, output = _invoke(
        [
            "fix", cluster.id, "--config", str(project), "--agent-cmd", AGENT_CMD,
            "--model", "fake/fixer", "--trials", "1", "--guard-suite", "smoke",
        ],
        generator,
    )  # fmt: skip

    assert code == exit_codes.CHECK_FAILED, output
    assert "# hecklr fix: nothing to fix" in output
    assert "passes every trial of the cluster's scenarios" in output
    assert generator.calls == []


@pytest.mark.parametrize(
    ("args", "message"),
    [
        (["--from-run", "latest"], "--agent-cmd is required"),
        (["--agent-cmd", "serve --port {port}"], "exactly one target"),
        (["CLUSTER", "--from-run", "latest", "--agent-cmd", "x {port}"], "exactly one target"),
        (["CLUSTER", "--tag", "a", "--agent-cmd", "x {port}"], "go with --from-run"),
        (["01K0000000000000000000000Z", "--agent-cmd", "x {port}"], "no cluster"),
        (["--from-run", "latest", "--agent-cmd", "x {port}", "--guard-suite", "nope"], "no suite"),
        (["--from-run", "latest", "--agent-cmd", "x {port}"], "no finished run"),
        (["--from-run", "latest", "--agent-cmd", "x {host}"], "must contain {port}"),
    ],
)
def test_configuration_problems_exit_78_before_anything_runs(
    tmp_path: Path, args: list[str], message: str
) -> None:
    project = _copy_project(tmp_path / "acme", fault=None)

    code, output = _invoke(["fix", "--config", str(project), *args])

    assert code == exit_codes.CONFIG_ERROR, output
    assert message in " ".join(output.split())
