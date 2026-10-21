"""Build the concrete adapter for an ``agent.adapters`` entry of ``hecklr.yaml``."""

from hecklr_adapters.errors import AdapterConfigError
from hecklr_adapters.http import HttpChatTarget
from hecklr_adapters.openai_compatible import OpenAICompatibleChatTarget
from hecklr_adapters.python import PythonChatTarget, load_agent
from hecklr_adapters.websocket import WebSocketChatTarget
from hecklr_schemas import (
    ChatAdapterConfig,
    ChatTarget,
    HttpAdapterConfig,
    OpenAICompatibleAdapterConfig,
    PythonAdapterConfig,
    WebhookAdapterConfig,
    WebSocketAdapterConfig,
)


def make_chat_target(config: ChatAdapterConfig) -> ChatTarget:
    """Return the ``ChatTarget`` for ``config``, validated so it fails before any trial.

    Raises:
        AdapterConfigError: the configuration cannot work (bad template, missing API key
            variable, unimportable entry point) or names an adapter not available yet.
    """
    match config:
        case HttpAdapterConfig():
            return HttpChatTarget(config)
        case OpenAICompatibleAdapterConfig():
            return OpenAICompatibleChatTarget(config)
        case PythonAdapterConfig():
            return PythonChatTarget(load_agent(config.entrypoint))
        case WebSocketAdapterConfig():
            return WebSocketChatTarget(config)
        case WebhookAdapterConfig():
            raise AdapterConfigError("the webhook chat adapter is not available yet (spec P1)")
