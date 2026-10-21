"""A tiny support agent behind a JSON HTTP endpoint, for exercising the ``http`` adapter for real.

It has no tools: it answers general questions and asks for a customer id, but cannot look up or
move a booking. Scenarios that need a tool call therefore fail, which is what a run against it
should show, with honest intervals.

Run it on its own with ``uvicorn http_agent:app --app-dir packages/hecklr/tests/fixtures``,
or start it in a test with ``serve_in_thread`` (the ``http_agent_url`` fixture).
"""

import re
import socket
import threading
from collections.abc import Iterator
from contextlib import contextmanager

import uvicorn
from fastapi import FastAPI
from pydantic import BaseModel

app = FastAPI(title="hecklr fixture agent")
_CUSTOMER_ID = re.compile(r"\bC-\d{4}\b", re.IGNORECASE)


class ChatRequest(BaseModel):
    message: str
    session_id: str


class ChatReply(BaseModel):
    reply: str


@app.post("/chat")
async def chat(request: ChatRequest) -> ChatReply:
    text = request.message.lower()
    if "saturday" in text or "weekend" in text:
        return ChatReply(reply="Yes, we do maintenance visits on Saturdays, from 9:00 to 13:00.")
    if "thank" in text or "bye" in text:
        return ChatReply(reply="You're welcome. Have a nice day!")
    if _CUSTOMER_ID.search(request.message):
        return ChatReply(reply="Thanks. Our booking system is offline; please call us back later.")
    return ChatReply(reply="Happy to help. What is your customer id? It looks like C-1234.")


@contextmanager
def serve_in_thread() -> Iterator[str]:
    """Serve the agent on an ephemeral 127.0.0.1 port; yields its chat URL."""
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
