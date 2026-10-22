"""acme-voice: the acme-maintenance support agent over voice (Hecklr's voice reference agent).

A cascaded pipeline (VAD -> ASR -> the acme brain from ``acme_agent`` -> TTS) served over the
``pcm`` WebSocket voice protocol. Its editable ``voice.yaml`` holds the knobs the voice fault
variants F7-F10 change.
"""

from acme_voice.config import (
    PROJECT_ROOT,
    PipelineConfig,
    VoiceConfigError,
    load_pipeline_config,
    voice_fault_overlay,
)
from acme_voice.normalize import normalize_transcript
from acme_voice.pipeline import GREETING, Mouth, SpeechStack, VoiceCall
from acme_voice.server import VoiceAgentServer, bound_port, running_server

__all__ = [
    "GREETING",
    "PROJECT_ROOT",
    "Mouth",
    "PipelineConfig",
    "SpeechStack",
    "VoiceAgentServer",
    "VoiceCall",
    "VoiceConfigError",
    "bound_port",
    "load_pipeline_config",
    "normalize_transcript",
    "running_server",
    "voice_fault_overlay",
]
