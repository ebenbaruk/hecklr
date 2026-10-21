"""The ``http`` chat adapter: a templated JSON POST per user message, optionally streamed.

Streaming modes (``HttpAdapterConfig.stream``):

- ``none``: one JSON (or plain text) response per message.
- ``sse``: server-sent events. Unnamed (``message``) events carry text deltas; ``data: [DONE]``
  ends the turn early. Named events report what the agent did, with the matching
  ``hecklr_schemas`` payload as JSON data: ``tool_call``, ``tool_result``, ``handoff``; and
  ``end`` says the agent closed the conversation.
- ``chunked``: a chunked body. Without ``response_path`` every chunk is a text delta; with it the
  body is newline-delimited JSON and each line's delta is read at that path.

Hecklr names each session with a fresh ULID, sent through the ``{{session_id}}`` placeholder;
the default template is ``{"message": "{{message}}", "session_id": "{{session_id}}"}``. Every
request also tells the agent where its trial's world sandbox is: headers, and the
``{{world_url}}`` / ``{{world_mcp_url}}`` placeholders (``hecklr_adapters.context``).
"""

from collections.abc import AsyncIterator
from typing import Final

import httpx2
from pydantic import JsonValue, ValidationError

from hecklr_adapters._http import (
    agent_http_errors,
    check_status,
    new_client,
    parse_json,
)
from hecklr_adapters.context import session_headers, template_values
from hecklr_adapters.errors import AgentError
from hecklr_adapters.templates import check_path, check_template, extract_text, render
from hecklr_schemas import (
    AgentChunk,
    HandoffPayload,
    HttpAdapterConfig,
    SessionContext,
    SessionSummary,
    ToolCallPayload,
    ToolResultPayload,
    new_id,
)

DEFAULT_REQUEST_TEMPLATE: Final[dict[str, JsonValue]] = {
    "message": "{{message}}",
    "session_id": "{{session_id}}",
}
SSE_DONE: Final = "[DONE]"


class HttpChatTarget:
    """Opens chat sessions with an agent behind a JSON HTTP endpoint."""

    def __init__(
        self, config: HttpAdapterConfig, *, transport: httpx2.AsyncBaseTransport | None = None
    ) -> None:
        """Validate the template and path up front, so a bad config fails before any trial.

        ``transport`` replaces the network, for tests.
        """
        self._template = config.request_template or DEFAULT_REQUEST_TEMPLATE
        check_template(self._template)
        if config.response_path is not None:
            check_path(config.response_path)
        self._config = config
        self._transport = transport

    async def open(self, ctx: SessionContext) -> "HttpChatSession":
        """Start a conversation; nothing is sent until the first message."""
        client = new_client(self._config.timeout_s, self._transport)
        return HttpChatSession(self._config, self._template, ctx, client)


class HttpChatSession:
    """One conversation over the ``http`` adapter."""

    def __init__(
        self,
        config: HttpAdapterConfig,
        template: JsonValue,
        ctx: SessionContext,
        client: httpx2.AsyncClient,
    ) -> None:
        self._config = config
        self._template = template
        self._ctx = ctx
        self._client = client
        self._url = str(config.url)
        self._session_id = new_id()
        self._turns = 0

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """POST ``text`` and stream the reply."""
        values = template_values(self._ctx, message=text, session_id=self._session_id)
        body = render(self._template, values)
        headers = {**self._config.headers, **session_headers(self._ctx)}
        with agent_http_errors(self._url, self._config.timeout_s):
            if self._config.stream == "sse":
                async for chunk in self._send_sse(body, headers):
                    yield chunk
            elif self._config.stream == "chunked":
                async for chunk in self._send_chunked(body, headers):
                    yield chunk
            else:
                response = await self._client.post(self._url, json=body, headers=headers)
                await check_status(response)
                yield AgentChunk(text=self._reply_text(response.text))
        self._turns += 1
        yield AgentChunk(final=True)

    async def close(self) -> SessionSummary:
        """Release the connection pool."""
        await self._client.aclose()
        return SessionSummary(turns=self._turns, agent_session_id=self._session_id)

    def _reply_text(self, body: str) -> str:
        path = self._config.response_path
        return body if path is None else extract_text(parse_json(body), path)

    async def _send_sse(
        self, body: JsonValue, headers: dict[str, str]
    ) -> AsyncIterator[AgentChunk]:
        async with self._client.sse(self._url, method="POST", json=body, headers=headers) as source:
            await check_status(source.response)
            async for event in source:
                if event.data == SSE_DONE:
                    return
                yield self._sse_chunk(event.event, event.data)

    def _sse_chunk(self, event: str, data: str) -> AgentChunk:
        try:
            match event:
                case "message":
                    return AgentChunk(text=self._reply_text(data))
                case "tool_call":
                    return AgentChunk(tool_call=ToolCallPayload.model_validate_json(data))
                case "tool_result":
                    return AgentChunk(tool_result=ToolResultPayload.model_validate_json(data))
                case "handoff":
                    return AgentChunk(handoff=HandoffPayload.model_validate_json(data or "{}"))
                case "end":
                    return AgentChunk(ended=True)
                case _:
                    raise AgentError(f"agent sent an unknown server-sent event type {event!r}")
        except ValidationError as exc:
            raise AgentError(f"agent sent a malformed {event!r} event: {exc}") from exc

    async def _send_chunked(
        self, body: JsonValue, headers: dict[str, str]
    ) -> AsyncIterator[AgentChunk]:
        async with self._client.stream("POST", self._url, json=body, headers=headers) as response:
            await check_status(response)
            if self._config.response_path is None:
                async for piece in response.aiter_text():
                    yield AgentChunk(text=piece)
                return
            async for line in response.aiter_lines():
                if line.strip():
                    yield AgentChunk(text=self._reply_text(line))
