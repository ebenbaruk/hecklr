"""The ``openai-compatible`` chat adapter: any agent behind ``POST {url}/chat/completions``.

The endpoint is stateless, so the session keeps the conversation history and resends it every
turn, exactly as an OpenAI-style client would.
"""

import os
from collections.abc import AsyncIterator, Mapping

import httpx2
from pydantic import JsonValue

from hecklr_adapters._http import agent_http_errors, check_status, new_client, parse_json
from hecklr_adapters.context import session_headers
from hecklr_adapters.errors import AdapterConfigError, AgentError
from hecklr_adapters.http import SSE_DONE
from hecklr_adapters.templates import extract
from hecklr_schemas import (
    AgentChunk,
    OpenAICompatibleAdapterConfig,
    SessionContext,
    SessionSummary,
)


class OpenAICompatibleChatTarget:
    """Opens chat sessions with an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        config: OpenAICompatibleAdapterConfig,
        *,
        transport: httpx2.AsyncBaseTransport | None = None,
        environ: Mapping[str, str] = os.environ,
    ) -> None:
        """Resolve the API key now, so a missing variable fails before any trial.

        ``transport`` replaces the network and ``environ`` the process environment, for tests.
        """
        self._config = config
        self._transport = transport
        self._auth: dict[str, str] = {}
        if config.api_key_env is not None:
            key = environ.get(config.api_key_env)
            if not key:
                raise AdapterConfigError(
                    f"environment variable {config.api_key_env} (agent api_key_env) is not set"
                )
            self._auth = {"Authorization": f"Bearer {key}"}

    async def open(self, ctx: SessionContext) -> "OpenAICompatibleChatSession":
        """Start a conversation with an empty history."""
        client = new_client(self._config.timeout_s, self._transport)
        headers = {**self._config.headers, **self._auth, **session_headers(ctx)}
        return OpenAICompatibleChatSession(self._config, headers, client)


class OpenAICompatibleChatSession:
    """One conversation; holds the message history the endpoint needs every turn."""

    def __init__(
        self,
        config: OpenAICompatibleAdapterConfig,
        headers: dict[str, str],
        client: httpx2.AsyncClient,
    ) -> None:
        self._config = config
        self._headers = headers
        self._client = client
        self._url = f"{str(config.url).rstrip('/')}/chat/completions"
        self._history: list[JsonValue] = []
        self._turns = 0

    async def send(self, text: str) -> AsyncIterator[AgentChunk]:
        """Send ``text`` with the history so far and stream the reply."""
        self._history.append({"role": "user", "content": text})
        body: dict[str, JsonValue] = {
            "messages": list(self._history),
            "stream": self._config.stream,
        }
        if self._config.model is not None:
            body["model"] = self._config.model
        reply: list[str] = []
        with agent_http_errors(self._url, self._config.timeout_s):
            if self._config.stream:
                async for delta in self._stream(body):
                    reply.append(delta)
                    yield AgentChunk(text=delta)
            else:
                response = await self._client.post(self._url, json=body, headers=self._headers)
                await check_status(response)
                reply.append(_content(parse_json(response.text), "message"))
                yield AgentChunk(text=reply[0])
        self._history.append({"role": "assistant", "content": "".join(reply)})
        self._turns += 1
        yield AgentChunk(final=True)

    async def close(self) -> SessionSummary:
        """Release the connection pool."""
        await self._client.aclose()
        return SessionSummary(turns=self._turns)

    async def _stream(self, body: JsonValue) -> AsyncIterator[str]:
        async with self._client.sse(
            self._url, method="POST", json=body, headers=self._headers
        ) as source:
            await check_status(source.response)
            async for event in source:
                if event.data == SSE_DONE:
                    return
                if delta := _content(parse_json(event.data), "delta"):
                    yield delta


def _content(completion: JsonValue, field: str) -> str:
    """Text of the first choice's ``message`` or ``delta``; empty when it carries none."""
    if isinstance(completion, dict) and "error" in completion:
        raise AgentError(f"agent reported an error: {completion['error']}")
    if isinstance(completion, dict) and completion.get("choices") == []:
        return ""  # e.g. a trailing usage-only chunk
    message = extract(completion, f"choices.0.{field}")
    if not isinstance(message, dict):
        raise AgentError(f"agent reply choices.0.{field} is not an object")
    content = message.get("content")
    if content is None:
        return ""
    if not isinstance(content, str):
        raise AgentError(f"agent reply content is {type(content).__name__}, not text")
    return content
