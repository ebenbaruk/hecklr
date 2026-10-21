"""``make_voice_target``: one entry point for every voice adapter configuration."""

import importlib
from typing import Any

import pytest

from hecklr_adapters import AdapterConfigError, make_voice_target
from hecklr_adapters.voice import WebSocketVoiceAdapterConfig, WebSocketVoiceTarget
from hecklr_adapters.voice.livekit import LiveKitVoiceTarget, _signal_url
from hecklr_adapters.voice.pipecat import PipecatVoiceTarget
from hecklr_schemas import AgentAdapters, SessionContext, TrialId, VoiceTarget, new_id

KEYS = {"LIVEKIT_API_KEY": "key", "LIVEKIT_API_SECRET": "secret"}


def voice(config: dict[str, Any]) -> Any:
    return AgentAdapters.model_validate({"voice": config}).voice


def test_websocket_config_builds_the_websocket_adapter() -> None:
    config = WebSocketVoiceAdapterConfig(type="websocket", url="ws://agent/call")
    target = make_voice_target(config)
    assert isinstance(target, WebSocketVoiceTarget)
    assert isinstance(target, VoiceTarget)


def test_pipecat_and_livekit_configs_build_their_adapters() -> None:
    pipecat = make_voice_target(voice({"type": "pipecat", "url": "http://bot/api/offer"}))
    assert isinstance(pipecat, PipecatVoiceTarget)
    livekit = make_voice_target(voice({"type": "livekit", "url": "https://lk.example"}), KEYS)
    assert isinstance(livekit, LiveKitVoiceTarget)
    ctx = SessionContext(trial_id=TrialId(new_id()), seed=1)
    assert livekit.room_name(ctx) == f"hecklr-{ctx.trial_id}"


def test_livekit_without_credentials_fails_before_any_trial() -> None:
    with pytest.raises(AdapterConfigError, match="LIVEKIT_API_SECRET"):
        make_voice_target(voice({"type": "livekit", "url": "ws://lk"}), {"LIVEKIT_API_KEY": "k"})


def test_livekit_signals_over_websocket_for_any_url_form() -> None:
    assert _signal_url("https://lk.example") == "wss://lk.example"
    assert _signal_url("http://127.0.0.1:7880") == "ws://127.0.0.1:7880"
    assert _signal_url("wss://lk.example") == "wss://lk.example"


@pytest.mark.parametrize(
    "config",
    [
        {"type": "webrtc", "url": "http://agent/whip"},
        {"type": "sip", "uri": "sip:agent@127.0.0.1"},
    ],
)
def test_adapters_not_built_yet_are_refused_clearly(config: dict[str, Any]) -> None:
    with pytest.raises(AdapterConfigError, match="not available yet"):
        make_voice_target(voice(config))


def test_a_missing_extra_is_a_configuration_error(monkeypatch: pytest.MonkeyPatch) -> None:
    def missing(name: str) -> Any:
        raise ImportError(f"No module named {name!r}")

    monkeypatch.setattr(importlib, "import_module", missing)
    with pytest.raises(AdapterConfigError, match=r"hecklr-adapters\[pipecat\]"):
        make_voice_target(voice({"type": "pipecat", "url": "http://bot/api/offer"}))
