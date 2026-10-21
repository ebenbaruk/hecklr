"""The ``livekit`` voice adapter: joins a LiveKit room as the caller (extra ``livekit``).

Each trial gets its own room, ``<room_prefix>-<trial_id>``, so a LiveKit agent dispatched to
new rooms answers exactly one call. The adapter signs its own access token with the API key and
secret named in the configuration, publishes the caller's microphone track, and treats the first
audio track another participant publishes as the agent's voice. The call is answered when that
track is subscribed; it ends when the agent leaves or the room closes.

LiveKit resamples the agent's track to 16 kHz mono itself; frames still go through a
:class:`~hecklr_adapters.voice.playout.Playout` so their ``sample_index`` follows arrival on the
receive clock, gaps included, exactly as with every other transport.
"""

import json
import os
from collections.abc import AsyncIterator, Mapping
from typing import Final

import anyio
import numpy as np
from livekit import api, rtc

from hecklr_adapters.errors import AdapterConfigError, AgentError
from hecklr_adapters.voice.audio import SAMPLE_RATE, pcm16
from hecklr_adapters.voice.playout import Playout, Receiver
from hecklr_adapters.voice.websocket import check_caller_rate
from hecklr_schemas import LiveKitAdapterConfig, SessionContext, TimedFrame

CALLER_TRACK: Final = "hecklr-caller"
_BACKLOG_S: Final = 0.02
"""Queued caller audio beyond which quiet frames are dropped to win the delay back."""
_QUIET_PEAK: Final = 100
"""Peak below -50 dBFS: no speech (the edge level ``hecklr-voice`` uses)."""
_LEFT_POLL_S: Final = 0.05
"""How often a quiet receive checks whether the agent has left the room."""


class LiveKitVoiceTarget:
    """Places each call by joining a fresh LiveKit room."""

    def __init__(
        self, config: LiveKitAdapterConfig, environ: Mapping[str, str] = os.environ
    ) -> None:
        """Read the API credentials now, so a missing variable fails before any trial."""
        missing = [
            name for name in (config.api_key_env, config.api_secret_env) if not environ.get(name)
        ]
        if missing:
            raise AdapterConfigError(
                f"livekit adapter needs {', '.join(missing)} in the environment"
            )
        self._config = config
        self._key = environ[config.api_key_env]
        self._secret = environ[config.api_secret_env]
        self._url = _signal_url(config.url)

    def room_name(self, ctx: SessionContext) -> str:
        """The room a trial's call happens in."""
        return f"{self._config.room_prefix}-{ctx.trial_id}"

    async def connect(self, ctx: SessionContext) -> "LiveKitAudioLink":
        """Join the trial's room, publish the caller and wait for the agent's audio."""
        room = rtc.Room()
        link = LiveKitAudioLink(room)
        try:
            await room.connect(self._url, self._token(ctx), rtc.RoomOptions(auto_subscribe=True))
            await link.start()
        except rtc.ConnectError as exc:
            await link.hangup()
            raise AgentError(f"cannot join LiveKit room at {self._url}: {exc}") from exc
        except BaseException:
            with anyio.CancelScope(shield=True):
                await link.hangup()
            raise
        return link

    def _token(self, ctx: SessionContext) -> str:
        grants = api.VideoGrants(room_join=True, room=self.room_name(ctx))
        return (
            api.AccessToken(self._key, self._secret)
            .with_identity(f"{CALLER_TRACK}-{ctx.trial_id}")
            .with_name("Hecklr caller")
            .with_metadata(json.dumps({"trial_id": ctx.trial_id}))
            .with_attributes({"hecklr.trial_id": ctx.trial_id})
            .with_grants(grants)
            .to_jwt()
        )


def _signal_url(url: str) -> str:
    """LiveKit signals over WebSocket; accept the HTTP form of the same address."""
    for http, ws in (("https://", "wss://"), ("http://", "ws://")):
        if url.startswith(http):
            return ws + url.removeprefix(http)
    return url


class LiveKitAudioLink:
    """One call in a LiveKit room."""

    def __init__(self, room: rtc.Room) -> None:
        self._room = room
        self._source = rtc.AudioSource(SAMPLE_RATE, 1)
        self._agent_track = anyio.Event()
        self._track: rtc.RemoteAudioTrack | None = None
        self._agent: str | None = None
        self._stream: rtc.AudioStream | None = None
        self._receiver = Receiver(Playout(), self._receive)
        self._left = False
        self._hung_up = False
        room.on("track_subscribed", self._on_track)
        room.on("participant_disconnected", self._on_participant_left)
        room.on("disconnected", self._on_disconnected)

    async def start(self) -> None:
        """Publish the caller's microphone, then wait until the agent's audio is subscribed."""
        track = rtc.LocalAudioTrack.create_audio_track(CALLER_TRACK, self._source)
        options = rtc.TrackPublishOptions(source=rtc.TrackSource.SOURCE_MICROPHONE)
        await self._room.local_participant.publish_track(track, options)
        await self._agent_track.wait()
        if self._track is None:
            raise AgentError("the room closed before the agent answered")
        self._stream = rtc.AudioStream.from_track(
            track=self._track, sample_rate=SAMPLE_RATE, num_channels=1
        )
        self._receiver.start()

    async def send_frame(self, pcm: bytes, sample_rate: int) -> None:
        """Feed one caller frame to the published track; frames go nowhere once the agent left."""
        check_caller_rate(sample_rate)
        if self._hung_up:
            raise AgentError("the call is over: it was hung up")
        if self._left:
            return
        # The source plays its queue out every 10 ms and plays silence when a frame comes late,
        # keeping the late frame queued behind it: each timing hiccup would add latency for the
        # rest of the call. A phone's sender drops silence to catch up; so does this one, and
        # never drops speech.
        samples = pcm16(pcm)
        quiet = int(np.abs(samples.astype(np.int32)).max(initial=0)) < _QUIET_PEAK
        if quiet and self._source.queued_duration > _BACKLOG_S:
            return
        await self._source.capture_frame(rtc.AudioFrame(pcm, SAMPLE_RATE, 1, len(samples)))

    def frames(self) -> AsyncIterator[TimedFrame]:
        """The agent's audio as heard in real time, until the agent leaves."""
        return self._receiver.frames()

    async def hangup(self) -> None:
        """Leave the room; safe to call twice."""
        if self._hung_up:
            return
        self._hung_up = True
        await self._receiver.stop()
        if self._stream is not None:
            await self._stream.aclose()
        await self._room.disconnect()
        await self._source.aclose()

    async def _receive(self) -> bool:
        stream = self._stream
        if stream is None:
            raise RuntimeError("receiving before the call was answered")
        # The stream does not end when the agent leaves, so a quiet wait checks for that.
        while not (self._left or self._hung_up):
            with anyio.move_on_after(_LEFT_POLL_S):
                try:
                    event = await stream.__anext__()
                except StopAsyncIteration:
                    return False
                self._receiver.playout.write(pcm16(event.frame.data.tobytes()))
                return True
        return False

    def _on_track(
        self,
        track: rtc.Track,
        publication: rtc.RemoteTrackPublication,
        participant: rtc.RemoteParticipant,
    ) -> None:
        if self._track is None and isinstance(track, rtc.RemoteAudioTrack):
            self._track = track
            self._agent = participant.identity
            self._agent_track.set()

    def _on_participant_left(self, participant: rtc.RemoteParticipant) -> None:
        if participant.identity == self._agent:
            self._left = True

    def _on_disconnected(self, reason: object) -> None:
        self._left = True
        self._agent_track.set()
