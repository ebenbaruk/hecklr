"""The ``WorldSandbox``: one in-process server hosting every trial's world for a run."""

import contextlib
import socket
from collections.abc import Generator
from contextlib import AsyncExitStack
from types import TracebackType
from typing import Literal, Self

import anyio
import uvicorn
from fastapi import FastAPI

from hecklr_schemas.trial import EventSink, TrialBrief
from hecklr_schemas.world import World
from hecklr_world.app import create_app
from hecklr_world.session import TrialWorld

ToolMode = Literal["sandbox", "observed"]


class _EmbeddedServer(uvicorn.Server):
    """A uvicorn server that leaves signal handling to the host process (the harness CLI)."""

    @contextlib.contextmanager
    def capture_signals(self) -> Generator[None]:
        yield


class Sandbox:
    """Serves one isolated world per trial from a single localhost server.

    Use as an async context manager; the server runs while the block is open::

        async with Sandbox(tool_mode=config.agent.tool_mode) as sandbox:
            world = await sandbox.open(brief, world_def, sink)
            ...
            await world.close()

    In ``sandbox`` mode each session gets ``mcp_url`` and ``http_url`` under ``/t/{trial_id}``.
    In ``observed`` mode sessions have no URLs; point the agent's OTLP exporter at
    ``otlp_endpoint`` instead.
    """

    def __init__(
        self,
        *,
        tool_mode: ToolMode = "sandbox",
        host: str = "127.0.0.1",
        port: int = 0,
        startup_timeout_s: float = 10.0,
    ) -> None:
        self._tool_mode = tool_mode
        self._host = host
        self._port = port
        self._startup_timeout_s = startup_timeout_s
        self._sessions: dict[str, TrialWorld] = {}
        self._stack: AsyncExitStack | None = None
        self._bound_port: int | None = None

    @property
    def tool_mode(self) -> ToolMode:
        """Whether agents call the sandbox (``sandbox``) or are observed through OTLP."""
        return self._tool_mode

    @property
    def base_url(self) -> str:
        """Root URL of the running server, e.g. ``http://127.0.0.1:53211``."""
        if self._bound_port is None:
            raise RuntimeError("the sandbox is not running; use `async with Sandbox() as ...`")
        return f"http://{self._host}:{self._bound_port}"

    @property
    def otlp_endpoint(self) -> str:
        """Value for the agent's ``OTEL_EXPORTER_OTLP_ENDPOINT`` (traces go to ``/v1/traces``)."""
        return self.base_url

    @property
    def open_trials(self) -> list[str]:
        """Trial ids with an open world."""
        return list(self._sessions)

    async def open(self, brief: TrialBrief, world: World, sink: EventSink) -> TrialWorld:
        """Load ``world`` fresh for this trial, seed its faults from ``brief.seed`` and serve it."""
        base = f"{self.base_url}/t/{brief.trial_id}"
        if brief.trial_id in self._sessions:
            raise ValueError(f"trial {brief.trial_id} already has an open world")
        observed = self._tool_mode == "observed"
        session = TrialWorld(
            brief,
            world,
            sink,
            observed=observed,
            mcp_url=None if observed else f"{base}/mcp",
            http_url=None if observed else base,
            on_close=self._forget,
        )
        self._sessions[brief.trial_id] = session
        return session

    def _forget(self, session: TrialWorld) -> None:
        self._sessions.pop(session.trial_id, None)

    def _bind(self) -> socket.socket:
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self._host, self._port))
        return sock

    def _build_server(self, app: FastAPI) -> _EmbeddedServer:
        config = uvicorn.Config(
            app,
            lifespan="on",
            # The harness owns logging: never let an embedded server reconfigure it.
            log_config=None,
            access_log=False,
            timeout_graceful_shutdown=5,
        )
        return _EmbeddedServer(config)

    async def __aenter__(self) -> Self:
        if self._stack is not None:
            raise RuntimeError("the sandbox is already running")
        sock = self._bind()
        port = sock.getsockname()[1]
        app = create_app(self._sessions, allowed_hosts=[f"{self._host}:{port}"])
        server = self._build_server(app)
        # Unwinds in reverse: close the sessions and ask the server to exit, wait for it to finish
        # (task group exit), then release the socket.
        stack = AsyncExitStack()
        stack.callback(sock.close)
        task_group = await stack.enter_async_context(anyio.create_task_group())
        stack.push_async_callback(self._shutdown, server)
        task_group.start_soon(server.serve, [sock])
        try:
            with anyio.fail_after(self._startup_timeout_s):
                while not server.started:  # noqa: ASYNC110 - uvicorn exposes readiness only as a flag
                    await anyio.sleep(0.005)
        except BaseException:
            await stack.aclose()
            raise
        self._stack, self._bound_port = stack, port
        return self

    async def _shutdown(self, server: _EmbeddedServer) -> None:
        for session in list(self._sessions.values()):
            await session.close()
        server.should_exit = True

    async def __aexit__(
        self,
        exc_type: type[BaseException] | None,
        exc: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        stack, self._stack, self._bound_port = self._stack, None, None
        if stack is not None:
            await stack.__aexit__(exc_type, exc, traceback)
