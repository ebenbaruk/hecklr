"""The ``websocket`` chat adapter: one socket per conversation, configurable message schema.

Each user message is sent as the rendered ``send_template`` (default
``{"message": "{{message}}", "session_id": "{{session_id}}"}``). Replies:

- no ``receive_path`` and no ``final_path``: each incoming message is one whole reply;
- ``receive_path``: incoming messages are JSON and the text delta is read at that path (a message
  without it, e.g. an end marker, adds no text);
- ``final_path`` (needs ``receive_path``): a boolean at that path marks the last message of the
  agent's turn; until it is true, messages are streamed deltas. Without it each message is a turn.
"""

import json
from collections.abc import AsyncIterator
from typing import Final

import anyio
from pydantic import JsonValue
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import WebSocketException

from hecklr_adapters._http import parse_json
from hecklr_adapters.errors import AdapterConfigError, AgentError, AgentTimeoutError
from hecklr_adapters.templates import check_path, check_template, find, render
from hecklr_schemas import (
    AgentChunk,
    SessionContext,
    SessionSummary,
    WebSocketAdapterConfig,
    new_id,
)

DEFAULT_SEND_TEMPLATE: Final[dict[str, JsonValue]] = {
    "message": "{{message}}",
    "session_id": "{{session_id}}",
}


class WebSocketChatTarget:
    """Opens one WebSocket connection per conversation."""

    def __init__(self, config: WebSocketAdapterConfig) -> None:
        """Validate the template and paths up front, so a bad config fails before any trial."""
        self._template = config.send_template or DEFAULT_SEND_TEMPLATE
        check_template(self._template)
        if config.final_path is not None and config.receive_path is None:
            raise AdapterConfigError("websocket final_path needs receive_path: replies are JSON")
        for path in (config.receive_path, config.final_path):
            if path is not None:
                check_path(path)
        self._config = config

    async def open(self, ctx: SessionContext) -> "WebSocketChatSession":
        """Connect, carrying the trial header on the handshake."""
        try:
            socket = await connect(
                self._config.url,
                additional_headers={**self._config.headers, **ctx.headers()},
                open_timeout=self._config.timeout_s,
            )
        except TimeoutError as exc:
            raise AgentTimeoutError(f"agent at {self._config.url} did not accept in time") from exc
        except (OSError, WebSocketException) as exc:
            raise AgentError(f"cannot connect to agent at {self._config.url}: {exc}") from exc
        return WebSocketChatSession(self._config, self._template, ctx, socket)


class WebSocketChatSession:
    """One conversation over an open WebSocket."""

    def __init__(
        self,
        config: WebSocketAdapterConfig,
        template: JsonValue,
        ctx: SessionContext,
        socket: ClientConnection,
    ) -> None:
        self._config = config
        self._template = template
        self._ctx = ctx
        self._socket = socket
        self._session_id = new_id()
        self._turns = 0

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """Send ``text`` and stream incoming messages until the turn is final."""
        values = {"message": text, "session_id": self._session_id, "trial_id": self._ctx.trial_id}
        try:
            await self._socket.send(json.dumps(render(self._template, values)))
            final = False
            while not final:
                text_delta, final = self._read(await self._receive())
                if text_delta:
                    yield AgentChunk(text=text_delta)
        except WebSocketException as exc:
            raise AgentError(f"agent connection failed: {exc}") from exc
        self._turns += 1
        yield AgentChunk(final=True)

    async def close(self) -> SessionSummary:
        """Close the socket with a normal closure."""
        await self._socket.close()
        return SessionSummary(turns=self._turns, agent_session_id=self._session_id)

    async def _receive(self) -> str:
        try:
            with anyio.fail_after(self._config.timeout_s):
                return await self._socket.recv(decode=True)
        except TimeoutError as exc:
            raise AgentTimeoutError(
                f"agent sent nothing for {self._config.timeout_s} s mid-turn"
            ) from exc

    def _read(self, message: str) -> tuple[str, bool]:
        """Text delta and whether this message ends the agent's turn."""
        receive_path, final_path = self._config.receive_path, self._config.final_path
        if receive_path is None:
            return message, True
        data = parse_json(message)
        final = find(data, final_path) if final_path is not None else True
        if not isinstance(final, bool | None):
            raise AgentError(f"agent message {final_path!r} is not a boolean")
        delta = find(data, receive_path)
        if not isinstance(delta, str | None):
            raise AgentError(f"agent message {receive_path!r} is not text")
        return delta or "", bool(final)
