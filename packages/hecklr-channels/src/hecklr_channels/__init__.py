"""Hecklr channel drivers: run one conversation over chat or email.

``ChatDriver(target)`` implements ``hecklr_schemas.ChannelDriver`` for chat; build the target with
``hecklr_adapters.make_chat_target``. ``EmailDriver`` does the same for email; build it from
config with ``make_email_driver``.
"""

from hecklr_channels.chat import ChatDriver
from hecklr_channels.email import EmailDriver, make_email_driver

__version__ = "0.1.0"

__all__ = ["ChatDriver", "EmailDriver", "make_email_driver"]
