"""voice-loopback: a networked calibration agent for Hecklr's voice engine (SC3, SC4).

``LoopbackServer`` answers every caller turn with a known reply a known delay after the caller's
last speech sample, and stops a known delay after a barge-in, over the ``pcm`` WebSocket voice
protocol. ``loopback_agent.bench`` drives it with Hecklr's real ``VoiceDriver`` and
``websocket`` adapter and compares what Hecklr measured with what the agent did.
"""

from loopback_agent.call import LoopbackCall, LoopbackSettings, Reply
from loopback_agent.server import LoopbackServer, bound_port, running_server

__all__ = [
    "LoopbackCall",
    "LoopbackServer",
    "LoopbackSettings",
    "Reply",
    "bound_port",
    "running_server",
]
