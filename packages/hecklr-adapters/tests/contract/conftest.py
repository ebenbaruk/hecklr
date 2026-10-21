"""Reference fake agents and the parameterized ``case`` fixture of the adapter contract suite.

Every chat adapter talks to the same reference agent behaviour over its own transport:

- it remembers the conversation and answers ``turn <n>: <message> (first: <first message>)``,
  streamed word by word where the transport streams;
- ``/error`` makes it fail the way its transport fails (HTTP 500, a raised exception, an
  abnormal WebSocket close); ``/slow`` makes it stall;
- it records the ``X-Hecklr-Trial-Id`` header of every message.

Servers run in a thread on an ephemeral 127.0.0.1 port. Tests that use them need sockets, so the
contract modules mark themselves ``enable_socket`` + ``allow_hosts(["127.0.0.1"])``: localhost
only, never the network. A voice adapter joins this suite (with a barge-in check) in WS10.
"""

import asyncio
import json
import re
import socket
import threading
import time
from collections import defaultdict
from collections.abc import AsyncIterator, Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field

import pytest
import uvicorn
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse, Response, StreamingResponse

from hecklr_adapters import (
    AgentSession,
    HttpChatTarget,
    OpenAICompatibleChatTarget,
    PythonAgent,
    PythonChatTarget,
    WebSocketChatTarget,
)
from hecklr_schemas import (
    TRIAL_ID_HEADER,
    AgentChunk,
    ChatTarget,
    HttpAdapterConfig,
    OpenAICompatibleAdapterConfig,
    WebSocketAdapterConfig,
)

ERROR = "/error"
SLOW = "/slow"
STALL_S = 1.5
TOKEN_GAP_S = 0.01
"""Pause between streamed words, so a chunked body really arrives in several chunks."""


def reply_text(user_messages: list[str]) -> str:
    """What the reference agent says after ``user_messages``."""
    return f"turn {len(user_messages)}: {user_messages[-1]} (first: {user_messages[0]})"


def words(text: str) -> list[str]:
    """Split into streamable pieces that join back to ``text``."""
    return re.findall(r"\S+\s*", text)


class AgentFailedError(Exception):
    """The reference agent was asked to fail."""


@dataclass
class ReferenceAgent:
    """Conversation memory and header log shared by every transport."""

    histories: dict[str, list[str]] = field(default_factory=lambda: defaultdict(list))
    trial_ids: list[str | None] = field(default_factory=list)

    def answer(self, session: str, message: str, trial_id: str | None) -> list[str]:
        """Record the message and return the reply as words."""
        self.trial_ids.append(trial_id)
        if message == ERROR:
            raise AgentFailedError("asked to fail")
        history = self.histories[session]
        history.append(message)
        return words(reply_text(history))


# --- HTTP and WebSocket reference agent ------------------------------------------------------


def build_app(agent: ReferenceAgent) -> FastAPI:
    """One FastAPI app serving every transport's flavour of the reference agent."""
    app = FastAPI()

    async def turn(request: Request, message: str, session: str) -> list[str] | Response:
        if message == SLOW:
            await asyncio.sleep(STALL_S)
        try:
            return agent.answer(session, message, request.headers.get(TRIAL_ID_HEADER))
        except AgentFailedError:
            return Response("internal error", status_code=500)

    @app.post("/chat")
    async def chat(request: Request) -> Response:
        body = await request.json()
        tokens = await turn(request, body["input"]["text"], body["conversation"])
        if isinstance(tokens, Response):
            return tokens
        return JSONResponse({"output": {"text": "".join(tokens)}})

    async def streamed(
        request: Request, frame: Callable[[str], str], done: str | None, media: str
    ) -> Response:
        body = await request.json()
        tokens = await turn(request, body["message"], body["session_id"])
        if isinstance(tokens, Response):
            return tokens

        async def frames() -> AsyncIterator[str]:
            for token in tokens:
                yield frame(token)
                await asyncio.sleep(TOKEN_GAP_S)
            if done is not None:
                yield done

        return StreamingResponse(frames(), media_type=media)

    @app.post("/sse")
    async def sse(request: Request) -> Response:
        return await streamed(
            request, lambda token: f"data: {token}\n\n", "data: [DONE]\n\n", "text/event-stream"
        )

    @app.post("/chunked")
    async def chunked(request: Request) -> Response:
        return await streamed(request, lambda token: token, None, "text/plain")

    @app.post("/ndjson")
    async def ndjson(request: Request) -> Response:
        return await streamed(
            request, lambda token: json.dumps({"delta": token}) + "\n", None, "application/x-ndjson"
        )

    @app.post("/v1/chat/completions")
    async def completions(request: Request) -> Response:
        body = await request.json()
        users = [m["content"] for m in body["messages"] if m["role"] == "user"]
        if users[-1] == SLOW:
            await asyncio.sleep(STALL_S)
        agent.trial_ids.append(request.headers.get(TRIAL_ID_HEADER))
        if users[-1] == ERROR:
            return JSONResponse({"error": {"message": "asked to fail"}}, status_code=500)
        text = reply_text(users)
        if not body["stream"]:
            return JSONResponse({"choices": [{"message": {"role": "assistant", "content": text}}]})

        async def events() -> AsyncIterator[str]:
            yield _sse({"choices": [{"delta": {"role": "assistant"}}]})
            for token in words(text):
                yield _sse({"choices": [{"delta": {"content": token}}]})
                await asyncio.sleep(TOKEN_GAP_S)
            yield _sse({"choices": [], "usage": {"completion_tokens": len(words(text))}})
            yield "data: [DONE]\n\n"

        return StreamingResponse(events(), media_type="text/event-stream")

    @app.websocket("/ws")
    async def ws(websocket: WebSocket) -> None:
        await _websocket_turns(websocket, agent, streamed=True)

    @app.websocket("/ws-whole")
    async def ws_whole(websocket: WebSocket) -> None:
        await _websocket_turns(websocket, agent, streamed=False)

    return app


def _sse(data: object) -> str:
    return f"data: {json.dumps(data)}\n\n"


async def _websocket_turns(websocket: WebSocket, agent: ReferenceAgent, *, streamed: bool) -> None:
    await websocket.accept()
    trial_id = websocket.headers.get(TRIAL_ID_HEADER)
    try:
        while True:
            body = json.loads(await websocket.receive_text())
            if body["message"] == SLOW:
                continue
            try:
                tokens = agent.answer(body["session_id"], body["message"], trial_id)
            except AgentFailedError:
                await websocket.close(code=1011)
                return
            if not streamed:
                await websocket.send_text("".join(tokens))
                continue
            for token in tokens:
                await websocket.send_json({"text": token, "done": False})
                await asyncio.sleep(TOKEN_GAP_S)
            await websocket.send_json({"done": True})
    except WebSocketDisconnect:
        return


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Run ``app`` in a thread on an ephemeral 127.0.0.1 port; yield ``host:port``."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    config = uvicorn.Config(app, log_level="warning", lifespan="off", timeout_graceful_shutdown=1)
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 5
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("reference agent server did not start")
        time.sleep(0.01)
    try:
        yield f"127.0.0.1:{sock.getsockname()[1]}"
    finally:
        server.should_exit = True
        thread.join(timeout=5)
        sock.close()


# --- in-process reference agent --------------------------------------------------------------


def python_agent(agent: ReferenceAgent, *, streamed: bool) -> PythonAgent:
    """The reference agent as an in-process callable, whole-reply or streamed."""

    async def whole(session: AgentSession, message: str) -> str:
        if message == SLOW:
            await asyncio.sleep(STALL_S)
        return "".join(agent.answer(session.id, message, session.context.trial_id))

    async def stream(session: AgentSession, message: str) -> AsyncIterator[str | AgentChunk]:
        if message == SLOW:
            await asyncio.sleep(STALL_S)
        for token in agent.answer(session.id, message, session.context.trial_id):
            yield AgentChunk(text=token)

    return stream if streamed else whole


# --- the parameterized case ------------------------------------------------------------------


@dataclass
class Case:
    """One adapter configuration under contract test."""

    name: str
    target: ChatTarget
    agent: ReferenceAgent
    streams: bool
    """A multi-word reply arrives in several text chunks."""
    timeout_s: float | None
    """The adapter's own network timeout; ``None`` if it has none (in-process)."""


CONTRACT_TIMEOUT_S = 0.5


def _build(name: str, agent: ReferenceAgent, address: str, timeout_s: float) -> Case:
    http = f"http://{address}"
    match name:
        case "http-json":
            config = HttpAdapterConfig.model_validate(
                {
                    "type": "http",
                    "url": f"{http}/chat",
                    "request_template": {
                        "input": {"text": "{{message}}"},
                        "conversation": "{{session_id}}",
                        "trial": "{{trial_id}}",
                    },
                    "response_path": "output.text",
                    "timeout_s": timeout_s,
                }
            )
            return Case(name, HttpChatTarget(config), agent, False, timeout_s)
        case "http-sse" | "http-chunked" | "http-ndjson":
            mode = name.removeprefix("http-")
            config = HttpAdapterConfig.model_validate(
                {
                    "type": "http",
                    "url": f"{http}/{mode}",
                    "stream": "sse" if mode == "sse" else "chunked",
                    "response_path": "delta" if mode == "ndjson" else None,
                    "timeout_s": timeout_s,
                }
            )
            return Case(name, HttpChatTarget(config), agent, True, timeout_s)
        case "openai" | "openai-stream":
            streamed = name == "openai-stream"
            openai_config = OpenAICompatibleAdapterConfig.model_validate(
                {
                    "type": "openai-compatible",
                    "url": f"{http}/v1",
                    "model": "reference",
                    "stream": streamed,
                    "timeout_s": timeout_s,
                }
            )
            return Case(name, OpenAICompatibleChatTarget(openai_config), agent, streamed, timeout_s)
        case "python" | "python-stream":
            streamed = name == "python-stream"
            target = PythonChatTarget(python_agent(agent, streamed=streamed))
            return Case(name, target, agent, streamed, None)
        case "websocket" | "websocket-whole":
            streamed = name == "websocket"
            ws_config = WebSocketAdapterConfig(
                type="websocket",
                url=f"ws://{address}/{'ws' if streamed else 'ws-whole'}",
                receive_path="text" if streamed else None,
                final_path="done" if streamed else None,
                timeout_s=timeout_s,
            )
            return Case(name, WebSocketChatTarget(ws_config), agent, streamed, timeout_s)
    raise ValueError(f"unknown contract case {name!r}")


CASES = [
    "http-json",
    "http-sse",
    "http-chunked",
    "http-ndjson",
    "openai",
    "openai-stream",
    "python",
    "python-stream",
    "websocket",
    "websocket-whole",
]


@pytest.fixture(params=CASES)
def case(request: pytest.FixtureRequest) -> Iterator[Case]:
    """Every chat adapter, wired to a fresh reference agent."""
    agent = ReferenceAgent()
    with serve(build_app(agent)) as address:
        yield _build(request.param, agent, address, CONTRACT_TIMEOUT_S)
