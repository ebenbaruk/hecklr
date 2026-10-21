"""Build the voice adapter for an ``agent.adapters.voice`` entry of ``hecklr.yaml``."""

import importlib
import os
from collections.abc import Mapping
from types import ModuleType
from typing import cast

from hecklr_adapters.errors import AdapterConfigError
from hecklr_adapters.voice.config import WebSocketVoiceAdapterConfig
from hecklr_adapters.voice.websocket import WebSocketVoiceTarget
from hecklr_schemas import (
    LiveKitAdapterConfig,
    PipecatAdapterConfig,
    SipAdapterConfig,
    VoiceAdapterConfig,
    VoiceTarget,
    WebRTCAdapterConfig,
)


def make_voice_target(
    config: VoiceAdapterConfig | WebSocketVoiceAdapterConfig,
    environ: Mapping[str, str] = os.environ,
) -> VoiceTarget:
    """Return the ``VoiceTarget`` for ``config``, validated so it fails before any trial.

    ``pipecat`` and ``livekit`` need the package extras of the same names; a missing extra
    or credential is a configuration error, not a trial failure.

    Raises:
        AdapterConfigError: the configuration cannot work here, or names an adapter that is
            not available yet (``webrtc``, ``sip``).
    """
    match config:
        case WebSocketVoiceAdapterConfig():
            return WebSocketVoiceTarget(config)
        case PipecatAdapterConfig():
            module = _extra("hecklr_adapters.voice.pipecat", "pipecat")
            return cast(VoiceTarget, module.PipecatVoiceTarget(config))
        case LiveKitAdapterConfig():
            module = _extra("hecklr_adapters.voice.livekit", "livekit")
            return cast(VoiceTarget, module.LiveKitVoiceTarget(config, environ))
        case WebRTCAdapterConfig() | SipAdapterConfig():
            raise AdapterConfigError(f"the {config.type} voice adapter is not available yet")


def _extra(module: str, extra: str) -> ModuleType:
    """Import an adapter whose transport library is an optional extra."""
    try:
        return importlib.import_module(module)
    except ImportError as exc:
        raise AdapterConfigError(
            f"the {extra} voice adapter needs `pip install 'hecklr-adapters[{extra}]'`: {exc}"
        ) from exc
