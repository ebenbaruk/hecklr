"""The caller's turn-taking decisions, drawn from persona behaviors and the trial seed.

The policy is pure: every decision is a function of (trial seed, decision kind, ordinal), so a
trial replays identically and adding one kind of decision never shifts another's numbers. The
call driver asks it what to do; it never touches audio or time itself.

- Normal turn: the caller answers ``response_gap_ms`` (persona range, default 200-900 ms) after
  the agent stops speaking.
- Barge-in: with ``interrupt_prob`` per agent reply, the caller cuts in at a random point
  :data:`BARGE_IN_WINDOW_MS` into the reply, if the agent is still talking by then.
- Backchannel: with ``backchannel_prob`` per agent reply, an "mm-hm" once the reply has run
  :data:`BACKCHANNEL_WINDOW_MS` long, i.e. only during long replies.
- Hesitation: with ``mid_utterance_pause_s`` set, an utterance of at least
  :data:`HESITATION_MIN_WORDS` words is split at a word boundary with probability
  :data:`HESITATION_PROB` and paused for a duration drawn from that range.
- Fillers: with ``fillers``, an utterance starts with "uh," or similar with probability
  :data:`FILLER_PROB`.
- Silence: with ``silence_prob`` per caller turn, the caller stays quiet 5-10 s first.
"""

from dataclasses import dataclass
from typing import Final

from hecklr_schemas import Behaviors
from hecklr_voice.audio import stream_rng

BARGE_IN_WINDOW_MS: Final = (400, 2500)
BACKCHANNEL_WINDOW_MS: Final = (1500, 3000)
RE_ENGAGEMENT_MS: Final = (5000, 10000)
HESITATION_PROB: Final = 0.5
HESITATION_MIN_WORDS: Final = 4
FILLER_PROB: Final = 0.3

FILLERS: Final = ("uh,", "um,", "hmm,", "uh, hold on,", "let me see,")
BACKCHANNELS: Final = ("mm-hm", "okay", "right", "yeah")
INTERJECTIONS: Final = (
    "Sorry, can I just say something?",
    "Wait, hold on a second.",
    "Sorry to interrupt, but that's not right.",
    "Hang on, let me stop you there.",
)


@dataclass(frozen=True, slots=True)
class UtterancePlan:
    """How the caller will say one message."""

    parts: tuple[str, ...]
    """One part, or two with a hesitation pause between them."""
    filler: str | None = None
    pause_ms: int | None = None
    """Hesitation between the two parts."""

    @property
    def text(self) -> str:
        """The message as the simulator wrote it (fillers excluded)."""
        return " ".join(self.parts)


@dataclass(frozen=True, slots=True)
class ReplyPlan:
    """What the caller will do while the agent speaks one reply (at most one of the two)."""

    barge_in_after_ms: int | None = None
    backchannel_after_ms: int | None = None


class TurnTakingPolicy:
    """Seeded turn-taking decisions for one trial."""

    def __init__(self, behaviors: Behaviors, seed: int) -> None:
        self._behaviors = behaviors
        self._seed = seed

    def plan_utterance(self, ordinal: int, text: str) -> UtterancePlan:
        """Fillers and hesitation for the caller's ``ordinal``-th message."""
        generator = stream_rng(self._seed, "utterance", ordinal)
        filler = None
        if self._behaviors.fillers and generator.random() < FILLER_PROB:
            filler = FILLERS[int(generator.integers(len(FILLERS)))]
        pause_range = self._behaviors.mid_utterance_pause_s
        words = text.split()
        if (
            pause_range is None
            or len(words) < HESITATION_MIN_WORDS
            or generator.random() >= HESITATION_PROB
        ):
            return UtterancePlan(parts=(text,), filler=filler)
        split = int(generator.integers(2, len(words) - 1))
        pause_ms = round(1000 * generator.uniform(*pause_range))
        parts = (" ".join(words[:split]), " ".join(words[split:]))
        return UtterancePlan(parts=parts, filler=filler, pause_ms=pause_ms)

    def response_gap_ms(self, ordinal: int) -> int:
        """Pause between the agent stopping and the caller's ``ordinal``-th message."""
        low, high = self._behaviors.response_gap_ms
        return int(stream_rng(self._seed, "gap", ordinal).integers(low, high + 1))

    def silence_ms(self, ordinal: int) -> int | None:
        """A 5-10 s re-engagement silence before the ``ordinal``-th message, or ``None``."""
        generator = stream_rng(self._seed, "silence", ordinal)
        if generator.random() >= self._behaviors.silence_prob:
            return None
        return int(generator.integers(RE_ENGAGEMENT_MS[0], RE_ENGAGEMENT_MS[1] + 1))

    def plan_reply(self, ordinal: int) -> ReplyPlan:
        """Barge-in or backchannel for the agent's ``ordinal``-th reply."""
        generator = stream_rng(self._seed, "reply", ordinal)
        if generator.random() < self._behaviors.interrupt_prob:
            low, high = BARGE_IN_WINDOW_MS
            return ReplyPlan(barge_in_after_ms=int(generator.integers(low, high + 1)))
        if generator.random() < self._behaviors.backchannel_prob:
            low, high = BACKCHANNEL_WINDOW_MS
            return ReplyPlan(backchannel_after_ms=int(generator.integers(low, high + 1)))
        return ReplyPlan()

    def interjection(self, ordinal: int) -> str:
        """What the caller says when barging in."""
        return INTERJECTIONS[
            int(stream_rng(self._seed, "interjection", ordinal).integers(len(INTERJECTIONS)))
        ]

    def backchannel(self, ordinal: int) -> str:
        """The caller's ``ordinal``-th backchannel."""
        return BACKCHANNELS[
            int(stream_rng(self._seed, "backchannel", ordinal).integers(len(BACKCHANNELS)))
        ]
