"""Hecklr channel drivers: run one conversation over chat (and, from WS12, email).

``ChatDriver(target)`` implements ``hecklr_schemas.ChannelDriver`` for chat; build the target with
``hecklr_adapters.make_chat_target``.
"""

from hecklr_channels.chat import ChatDriver

__version__ = "0.1.0"

__all__ = ["ChatDriver"]
