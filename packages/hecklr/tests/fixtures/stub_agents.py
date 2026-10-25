"""Broken agents behind a JSON HTTP endpoint, as the red team's repros used them (#33-#35).

Each factory returns an ASGI app whose ``POST /chat`` misbehaves in one way; ``serve`` runs any
of them on an ephemeral 127.0.0.1 port.
"""

import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import anyio
import uvicorn
from fastapi import FastAPI
from fastapi.responses import PlainTextResponse


def failing_agent() -> FastAPI:
    """Answers every message with HTTP 500."""
    app = FastAPI()

    @app.post("/chat")
    async def chat() -> PlainTextResponse:
        return PlainTextResponse("boom", status_code=500)

    return app


def flooding_agent(reply_bytes: int) -> FastAPI:
    """Answers every message with ``{"reply": "AAA..."}`` of ``reply_bytes`` characters."""
    app = FastAPI()
    body = b'{"reply": "' + b"A" * reply_bytes + b'"}'

    @app.post("/chat")
    async def chat() -> PlainTextResponse:
        return PlainTextResponse(body, media_type="application/json")

    return app


def slow_agent(delay_s: float, asked: threading.Event | None = None) -> FastAPI:
    """Answers every message with ``{"reply": "ok"}`` after ``delay_s`` seconds; sets ``asked``
    when a message arrives, so a test can tell that a trial is in flight."""
    app = FastAPI()

    @app.post("/chat")
    async def chat() -> dict[str, str]:
        if asked is not None:
            asked.set()
        await anyio.sleep(delay_s)
        return {"reply": "ok"}

    return app


@contextmanager
def serve(app: FastAPI) -> Iterator[str]:
    """Serve ``app`` on an ephemeral 127.0.0.1 port; yields its chat URL."""
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, log_config=None, access_log=False))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    try:
        while not server.started:
            threading.Event().wait(0.01)
        yield f"http://127.0.0.1:{port}/chat"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
