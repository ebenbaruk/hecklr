import socket
from pathlib import Path

import pytest

from hecklr_fix import agent
from hecklr_fix.agent import AgentCommand, free_port
from hecklr_fix.errors import FixError

# The agents under test listen on 127.0.0.1 ephemeral ports.
pytestmark = [pytest.mark.anyio, pytest.mark.enable_socket, pytest.mark.allow_hosts(["127.0.0.1"])]

SERVER = """\
import signal, socket, sys, time
if sys.argv[2] == "ignore-term":
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
if sys.argv[2] == "exit":
    sys.exit(7)
if sys.argv[2] != "never-listen":
    server = socket.socket()
    server.bind(("127.0.0.1", int(sys.argv[1])))
    server.listen()
print("agent up", flush=True)
time.sleep(60)
"""


def _listening(port: int) -> bool:
    with socket.socket() as probe:
        return probe.connect_ex(("127.0.0.1", port)) == 0


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "server.py").write_text(SERVER, encoding="utf-8")
    return tmp_path


async def test_the_agent_runs_from_the_workspace_until_the_block_ends(workspace: Path) -> None:
    command = AgentCommand("{python} server.py {port} serve")
    port = free_port()
    log = workspace / "logs" / "agent.log"

    async with command.launch(workspace, "127.0.0.1", port, log):
        assert _listening(port)

    assert not _listening(port)
    assert "agent up" in log.read_text()


async def test_an_agent_ignoring_sigterm_is_killed(
    workspace: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(agent, "STOP_GRACE_S", 0.5)
    port = free_port()

    async with AgentCommand("{python} {project}/server.py {port} ignore-term").launch(
        workspace, "127.0.0.1", port, workspace / "a.log"
    ):
        assert _listening(port)

    assert not _listening(port)


async def test_an_agent_that_exits_before_listening_is_reported(workspace: Path) -> None:
    with pytest.raises(FixError, match="exited with code 7 before listening"):
        async with AgentCommand("{python} server.py {port} exit").launch(
            workspace, "127.0.0.1", free_port(), workspace / "a.log"
        ):
            pytest.fail("the block must not run")


async def test_an_agent_that_never_listens_times_out(workspace: Path) -> None:
    command = AgentCommand("{python} server.py {port} never-listen", ready_timeout_s=0.5)
    with pytest.raises(FixError, match=r"did not listen on 127\.0\.0\.1"):
        async with command.launch(workspace, "127.0.0.1", free_port(), workspace / "a.log"):
            pytest.fail("the block must not run")


async def test_a_missing_executable_is_a_fix_error(workspace: Path) -> None:
    with pytest.raises(FixError, match="cannot start the agent"):
        async with AgentCommand("./no-such-agent --port {port}").launch(
            workspace, "127.0.0.1", free_port(), workspace / "a.log"
        ):
            pytest.fail("the block must not run")


def test_templates_are_checked_before_any_run() -> None:
    with pytest.raises(FixError, match="must contain"):
        AgentCommand("serve --port 8000")
    with pytest.raises(FixError, match=r"bad placeholder 'host'"):
        AgentCommand("serve --host {host} --port {port}")
    assert AgentCommand("serve --port {port} --json '{{}}'").argv(port=5, project=Path("/w")) == [
        "serve",
        "--port",
        "5",
        "--json",
        "{}",
    ]
