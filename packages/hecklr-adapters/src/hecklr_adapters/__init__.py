"""Hecklr target adapters: the only code that knows how to reach a specific agent.

Chat adapters implement ``hecklr_schemas.ChatTarget``; build one from config with
``make_chat_target``. Voice adapters (``hecklr_adapters.voice``) implement ``VoiceTarget``;
build one with ``make_voice_target``. Adapters raise ``AgentError`` (or
``AgentTimeoutError``) when the agent fails; any other exception is a harness failure.
"""

from hecklr_adapters.context import WORLD_MCP_URL_HEADER, WORLD_URL_HEADER, session_headers
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
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, make_voice_target
from hecklr_adapters.websocket import WebSocketChatSession, WebSocketChatTarget

__version__ = "0.1.0"

__all__ = [
    "WORLD_MCP_URL_HEADER",
    "WORLD_URL_HEADER",
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
    "WebSocketVoiceAdapterConfig",
    "load_agent",
    "make_chat_target",
    "make_voice_target",
    "session_headers",
]
