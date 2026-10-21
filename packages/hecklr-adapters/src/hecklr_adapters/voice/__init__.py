"""Voice adapters: implementations of ``hecklr_schemas.VoiceTarget`` / ``AudioLink``.

Every adapter delivers the agent's audio as 16 kHz ``TimedFrame`` s, played out in real time
with sample-accurate ``sample_index`` on the receive stream's media clock (ADR 0009a), and takes
the caller's 20 ms 16 kHz frames. Build one from config with ``make_voice_target``.
"""

from hecklr_adapters.voice.config import WebSocketVoiceAdapterConfig
from hecklr_adapters.voice.factory import make_voice_target
from hecklr_adapters.voice.playout import Playout
from hecklr_adapters.voice.websocket import WebSocketAudioLink, WebSocketVoiceTarget

__all__ = [
    "Playout",
    "WebSocketAudioLink",
    "WebSocketVoiceAdapterConfig",
    "WebSocketVoiceTarget",
    "make_voice_target",
]
