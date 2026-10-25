"""Hecklr target adapters: the only code that knows how to reach a specific agent.

Chat adapters implement ``hecklr_schemas.ChatTarget``; build one from config with
``make_chat_target``. Voice adapters (``hecklr_adapters.voice``) implement ``VoiceTarget``;
build one with ``make_voice_target``. Email adapters (``hecklr_adapters.email``) implement
``hecklr_schemas.EmailTarget``; build one with ``make_email_target``. Adapters raise
``AgentError`` (or ``AgentTimeoutError``) when the agent fails; any other exception is a harness
failure. ``cap_reply_size`` bounds the text of one agent turn around any chat target.
"""

from hecklr_adapters.context import (
    WORLD_MCP_URL_HEADER,
    WORLD_URL_HEADER,
    session_fields,
    session_headers,
)
from hecklr_adapters.email import make_email_target
from hecklr_adapters.errors import AdapterConfigError, AgentError, AgentTimeoutError
from hecklr_adapters.factory import make_chat_target
from hecklr_adapters.headers import resolve_headers
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
from hecklr_adapters.reply_cap import DEFAULT_MAX_REPLY_BYTES, cap_reply_size
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, make_voice_target
from hecklr_adapters.websocket import WebSocketChatSession, WebSocketChatTarget

__version__ = "0.1.0"

__all__ = [
    "DEFAULT_MAX_REPLY_BYTES",
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
    "cap_reply_size",
    "load_agent",
    "make_chat_target",
    "make_email_target",
    "make_voice_target",
    "resolve_headers",
    "session_fields",
    "session_headers",
]
