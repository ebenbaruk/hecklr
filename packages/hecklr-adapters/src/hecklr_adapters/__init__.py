"""Hecklr target adapters: the only code that knows how to reach a specific agent.

Chat adapters implement ``hecklr_schemas.ChatTarget``; build one from config with
``make_chat_target``. Email adapters (``hecklr_adapters.email``) implement
``hecklr_schemas.EmailTarget``; build one with ``make_email_target``. Adapters raise
``AgentError`` (or ``AgentTimeoutError``) when the agent fails; any other exception is a harness
failure.
"""

from hecklr_adapters.email import make_email_target
from hecklr_adapters.errors import AdapterConfigError, AgentError, AgentTimeoutError
from hecklr_adapters.factory import make_chat_target
from hecklr_adapters.http import HttpChatSession, HttpChatTarget
from hecklr_adapters.openai_compatible import (
    OpenAICompatibleChatSession,
    OpenAICompatibleChatTarget,
)
from hecklr_adapters.python import (
    AgentSession,
    PythonAgent,
    PythonChatSession,
    PythonChatTarget,
    load_agent,
)
from hecklr_adapters.websocket import WebSocketChatSession, WebSocketChatTarget

__version__ = "0.1.0"

__all__ = [
    "AdapterConfigError",
    "AgentError",
    "AgentSession",
    "AgentTimeoutError",
    "HttpChatSession",
    "HttpChatTarget",
    "OpenAICompatibleChatSession",
    "OpenAICompatibleChatTarget",
    "PythonAgent",
    "PythonChatSession",
    "PythonChatTarget",
    "WebSocketChatSession",
    "WebSocketChatTarget",
    "load_agent",
    "make_chat_target",
    "make_email_target",
]
