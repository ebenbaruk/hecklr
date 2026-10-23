"""SC1: the quickstart, executed literally, gives a first report in under 5 minutes.

SC1 is checked twice. First the three commands the README and the docs site share, read from
both pages and run in a fresh directory through the installed ``hecklr`` console script, as a
user would. Then the reference agent: ``acme-maintenance`` started with the command from "Try the
reference agent", and the quickstart's "Point it at your agent" commands run against it from
another fresh directory, all of it (agent start included) inside the same 5 minutes.

``pip install hecklr`` is the one step not run: the test environment is that install (Hecklr is
not on PyPI yet; see the quickstart's "Before the first PyPI release" note). For the same reason
``uv run python`` becomes this interpreter.

It reproduces the quickstart on a laptop with no model key and no local Ollama, where ``hecklr
init`` picks scripted ``fake/`` models: no network and no cost. Keys are removed from the
environment, and the proxy variables make ``localhost:11434`` unreachable on a developer machine
that runs Ollama (``hecklr init`` probes that fixed URL), while ``127.0.0.1`` (the world sandbox
and the reference agent) stays direct. That is why the documented ``localhost:8000`` becomes
``127.0.0.1`` and a free port, the only change made to the documented commands.
"""

import os
import re
import shlex
import socket
import subprocess
import sys
import time
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from docs_snippets import DOCS, REPO, commands, marked_block

SC1_LIMIT_S = 300
# The agent's start counts against SC1's 5 minutes, so it gets all of them, not a tighter limit
# that a loaded CI runner could trip before SC1 itself is in doubt.
_AGENT_START_TIMEOUT_S = SC1_LIMIT_S
_DOCUMENTED_AGENT = "http://localhost:8000"
_MODEL_KEYS = ("OPENAI_API_KEY", "ANTHROPIC_API_KEY")


def _offline_laptop_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in _MODEL_KEYS}
    env.update(
        HTTP_PROXY="http://127.0.0.1:9",
        http_proxy="http://127.0.0.1:9",
        NO_PROXY="127.0.0.1",
        no_proxy="127.0.0.1",
        NO_COLOR="1",
        COLUMNS="160",
    )
    return env


def _hecklr_argv(command: str) -> list[str]:
    argv = shlex.split(command)
    assert argv[0] == "hecklr", f"the quickstart runs hecklr commands only, got {command!r}"
    return [str(Path(sys.executable).with_name("hecklr")), *argv[1:]]


def _run_steps(steps: list[str], cwd: Path) -> list[str]:
    """Run documented ``hecklr`` commands in order; their stdout, each one asserted to succeed."""
    outputs = []
    for step in steps:
        done = subprocess.run(  # noqa: S603 -- the documented command, run as a user would
            _hecklr_argv(step),
            cwd=cwd,
            env=_offline_laptop_env(),
            capture_output=True,
            text=True,
            timeout=SC1_LIMIT_S,
            check=False,
        )
        assert done.returncode == 0, f"{step} exited {done.returncode}:\n{done.stdout}{done.stderr}"
        outputs.append(done.stdout)
    return outputs


def _assert_first_report(run_output: str, project: Path) -> None:
    assert "Run metrics (95% CI)" in run_output
    assert "Scenarios (Wilson 95% CI)" in run_output
    report = re.search(r"Report: (\S+report\.html)", run_output)
    assert report, run_output
    path = Path(report.group(1))
    assert path.is_relative_to(project)
    assert "<html" in path.read_text(encoding="utf-8").lower()


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port: int = probe.getsockname()[1]
        return port


def _wait_healthy(agent: subprocess.Popen[bytes], base_url: str) -> None:
    # No proxy handler: the developer's proxy variables must not intercept a localhost probe.
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    deadline = time.monotonic() + _AGENT_START_TIMEOUT_S
    while time.monotonic() < deadline:
        assert agent.poll() is None, f"the reference agent exited with {agent.returncode}"
        try:
            with opener.open(f"{base_url}/healthz", timeout=1) as response:
                if response.status == 200:
                    return
        except OSError:
            time.sleep(0.2)
    pytest.fail(f"the reference agent did not answer {base_url}/healthz in time")


@contextmanager
def _reference_agent(command: str, port: int, log: Path) -> Iterator[str]:
    """Start the documented reference agent on ``port``; yield its base URL; stop it."""
    argv = shlex.split(command)
    assert argv[:3] == ["uv", "run", "python"], command
    assert "--port" in argv, command
    argv[argv.index("--port") + 1] = str(port)
    base_url = f"http://127.0.0.1:{port}"
    with log.open("wb") as output:
        agent = subprocess.Popen(  # noqa: S603 -- the documented command, in this interpreter
            [sys.executable, *argv[3:]], cwd=REPO, stdout=output, stderr=subprocess.STDOUT
        )
        try:
            _wait_healthy(agent, base_url)
            yield base_url
        finally:
            agent.terminate()
            try:
                agent.wait(timeout=10)
            except subprocess.TimeoutExpired:
                agent.kill()
                agent.wait()


@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.slow
def test_sc1_quickstart_init_to_first_report_under_5_minutes(tmp_path: Path) -> None:
    readme = commands(marked_block(REPO / "README.md", "quickstart"))
    docs = commands(marked_block(DOCS / "quickstart.md", "quickstart"))
    assert readme == docs, "README and docs/quickstart.md must show the same quickstart"
    install, *steps = docs
    assert install == "pip install hecklr"
    assert len(steps) == 2, steps

    started = time.monotonic()
    init_output, run_output = _run_steps(steps, tmp_path)
    elapsed = time.monotonic() - started

    assert "scripted fake models" in init_output  # the offline laptop the test stands for
    _assert_first_report(run_output, tmp_path)
    assert elapsed < SC1_LIMIT_S, f"init to first report took {elapsed:.0f} s"


@pytest.mark.e2e
@pytest.mark.fake
@pytest.mark.slow
@pytest.mark.enable_socket  # localhost only: a free port for the agent and its /healthz probe
def test_sc1_quickstart_on_reference_agent_under_5_minutes(tmp_path: Path) -> None:
    start_agent = commands(marked_block(DOCS / "quickstart.md", "reference-agent"))[0]
    steps = commands(marked_block(DOCS / "quickstart.md", "agent-url"))
    assert "examples/acme-maintenance/agent/serve.py --brain rules" in start_agent
    assert len(steps) == 2, steps
    assert f"--agent-url {_DOCUMENTED_AGENT}/chat" in steps[0], steps[0]
    project = tmp_path / "project"
    project.mkdir()

    started = time.monotonic()
    with _reference_agent(start_agent, _free_port(), tmp_path / "agent.log") as agent_url:
        init_output, run_output = _run_steps(
            [step.replace(_DOCUMENTED_AGENT, agent_url) for step in steps], project
        )
    elapsed = time.monotonic() - started

    assert "using the http adapter" in init_output
    assert f'"url": "{agent_url}/chat"' in (project / "hecklr.yaml").read_text(encoding="utf-8")
    _assert_first_report(run_output, project)
    assert elapsed < SC1_LIMIT_S, f"agent start to first report took {elapsed:.0f} s"
