"""The ``livekit`` adapter's release of a call, without a server."""

import pytest
from livekit import rtc

from hecklr_adapters.voice.livekit import LiveKitAudioLink

pytestmark = pytest.mark.anyio


async def test_hangup_leaves_the_room_even_when_a_step_fails(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    room = rtc.Room()
    link = LiveKitAudioLink(room)
    released: list[str] = []

    async def broken_stop() -> None:
        raise RuntimeError("reader would not stop")

    async def disconnect() -> None:
        released.append("room")

    async def close_source() -> None:
        released.append("source")

    monkeypatch.setattr(link._receiver, "stop", broken_stop)
    monkeypatch.setattr(room, "disconnect", disconnect)
    monkeypatch.setattr(link._source, "aclose", close_source)
    with pytest.raises(RuntimeError, match="would not stop"):
        await link.hangup()
    assert released == ["room", "source"]
    await link.hangup()  # released once already
    assert released == ["room", "source"]
