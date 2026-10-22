"""The loopback agent's ears and timing for one call, free of any transport.

The agent hears the caller's audio as chunks arriving over the network. It places every chunk on
the caller's media clock with earliest-arrival alignment (as an RTP receiver does), so a chunk
delivered late by the network or by this process's own scheduling never moves the timing it
decides on. It then:

- answers ``reply_after_ms`` after the caller's last speech sample, once the caller has been
  quiet for ``endpoint_ms``;
- stops a reply ``barge_in_reaction_ms`` after the first speech sample of a caller who talks
  over it (never, when that is ``None``).

Both are the truths Hecklr must measure within 20 ms (SC4). Times are milliseconds on this
process's monotonic clock.
"""

from dataclasses import dataclass, field
from typing import Final

from hecklr_voice.audio import SAMPLE_RATE, SAMPLES_PER_MS, Pcm16, frame_dbfs, speech_edges
from hecklr_voice.vad import DEFAULT_ENERGY_THRESHOLD_DBFS

GREETING_AFTER_MS: Final = 200.0
"""How long after the call starts the optional greeting begins."""
DEFAULT_REPLY_TEXT: Final = (
    "sure I can move your boiler visit to any morning next week that suits you"
)
"""About 3 s of speech: long enough for a caller to barge in, short enough for quick calls."""


@dataclass(frozen=True, slots=True)
class LoopbackSettings:
    """The injected timings; every call of a server shares them."""

    reply_after_ms: float = 600.0
    """Response delay, from the caller's last speech sample to the reply's first sample."""
    barge_in_reaction_ms: float | None = 200.0
    """Delay from a barge-in's first speech sample to the reply stopping; ``None`` never stops."""
    endpoint_ms: float = 200.0
    """Caller silence after which the agent decides the caller has finished."""
    greeting: bool = False
    """Say the reply once, ``GREETING_AFTER_MS`` after the call starts."""
    reply_text: str = DEFAULT_REPLY_TEXT
    """What every reply says (tagged synthetic speech, which ``FakeASR`` reads exactly)."""

    def __post_init__(self) -> None:
        if self.endpoint_ms <= 0:
            raise ValueError("endpoint_ms must be positive")
        if self.reply_after_ms < self.endpoint_ms + 20:
            # The reply is scheduled when the endpoint is reached, so it cannot be due earlier.
            raise ValueError("reply_after_ms must exceed endpoint_ms by at least one frame (20 ms)")
        if self.barge_in_reaction_ms is not None and self.barge_in_reaction_ms <= 0:
            raise ValueError("barge_in_reaction_ms must be positive (or None: never stop)")


@dataclass(slots=True)
class Reply:
    """One reply: when it was planned to play, and how it actually went out."""

    start_ms: float
    duration_ms: float
    cut_ms: float | None = None
    """When the reply must stop because the caller barged in."""
    late_ms: float | None = None
    """How much later than ``start_ms`` its first audio left; set by the speaker."""

    @property
    def end_ms(self) -> float:
        """When the reply stops: its cut, or its natural end."""
        natural = self.start_ms + self.duration_ms
        return natural if self.cut_ms is None else min(natural, self.cut_ms)


@dataclass(slots=True)
class LoopbackCall:
    """What the agent has heard on one call and the replies it has decided on."""

    settings: LoopbackSettings
    reply_ms: float
    """Length of the reply audio."""
    started_ms: float
    replies: list[Reply] = field(default_factory=list)
    _origin_ms: float | None = None
    _heard: int = 0
    """Caller samples heard so far: the position on the caller's media clock."""
    _speaking: bool = False
    _last_speech_ms: float = 0.0

    def __post_init__(self) -> None:
        if self.settings.greeting:
            self.replies.append(Reply(self.started_ms + GREETING_AFTER_MS, self.reply_ms))

    def hear(self, samples: Pcm16, arrival_ms: float) -> bool:
        """Hear one chunk of 16 kHz caller audio; return whether a new reply was scheduled."""
        origin = arrival_ms - self._heard / SAMPLES_PER_MS
        self._origin_ms = origin if self._origin_ms is None else min(self._origin_ms, origin)
        chunk_ms = self._origin_ms + self._heard / SAMPLES_PER_MS
        self._heard += len(samples)
        speech = len(samples) > 0 and frame_dbfs(samples) >= DEFAULT_ENERGY_THRESHOLD_DBFS
        edges = speech_edges(samples) if speech else None
        if edges is not None:
            onset_ms = chunk_ms + edges[0] / SAMPLES_PER_MS
            if not self._speaking:
                self._speaking = True
                self._caller_started(onset_ms)
            self._last_speech_ms = chunk_ms + edges[1] / SAMPLES_PER_MS
            return False
        if self._speaking and chunk_ms - self._last_speech_ms >= self.settings.endpoint_ms:
            self._speaking = False
            busy_until = max((reply.end_ms for reply in self.replies), default=0.0)
            start = max(self._last_speech_ms + self.settings.reply_after_ms, busy_until)
            self.replies.append(Reply(start, self.reply_ms))
            return True
        return False

    def _caller_started(self, onset_ms: float) -> None:
        reaction = self.settings.barge_in_reaction_ms
        if reaction is None:
            return
        for reply in self.replies:
            if reply.start_ms <= onset_ms < reply.end_ms:
                reply.cut_ms = onset_ms + reaction


def duration_ms(samples: Pcm16) -> float:
    """Length of 16 kHz audio in milliseconds."""
    return len(samples) * 1000 / SAMPLE_RATE
