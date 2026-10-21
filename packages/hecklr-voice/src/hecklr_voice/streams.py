"""The two sides of a call as the harness sees them: what the caller sends, what the agent says.

``CallerStream`` decides the content of every outgoing 20 ms frame: a scheduled clip, or the
idle line (silence, or background noise on a noisy persona). ``AgentSpeech`` turns incoming
frames into speech segments with sample-accurate edges. Both are plain state machines driven by
the call's tasks; neither sleeps or does I/O.
"""

from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field

import anyio

from hecklr_voice.audio import FRAME_SAMPLES, SAMPLES_PER_MS, Pcm16, speech_edges


@dataclass(slots=True)
class Clip:
    """Caller audio scheduled on the send stream."""

    pcm: Pcm16
    start_frame: int
    done: anyio.Event = field(default_factory=anyio.Event)

    @property
    def end_frame(self) -> int:
        """First frame after the clip."""
        return self.start_frame + len(self.pcm) // FRAME_SAMPLES


class CallerStream:
    """Frame-by-frame content of the caller's side: queued clips, else the idle line."""

    def __init__(self, idle: Pcm16) -> None:
        if len(idle) == 0 or len(idle) % FRAME_SAMPLES:
            raise ValueError("the idle line must be a non-empty whole number of frames")
        self._idle = idle
        self._clips: deque[Clip] = deque()
        self.next_frame = 0
        """The first frame whose content has not been taken yet."""

    def play(self, pcm: Pcm16) -> Clip:
        """Queue ``pcm`` (whole frames) to start as soon as the line is free."""
        if len(pcm) % FRAME_SAMPLES:
            raise ValueError("clips must be a whole number of frames")
        start = max(self.next_frame, self._clips[-1].end_frame if self._clips else 0)
        clip = Clip(pcm, start)
        self._clips.append(clip)
        return clip

    def take(self, index: int) -> Pcm16:
        """The content of frame ``index``; frames must be taken in order."""
        self.next_frame = index + 1
        while self._clips and self._clips[0].end_frame <= index:
            self._clips.popleft().done.set()
        if self._clips and self._clips[0].start_frame <= index:
            clip = self._clips[0]
            offset = (index - clip.start_frame) * FRAME_SAMPLES
            frame = clip.pcm[offset : offset + FRAME_SAMPLES]
            if clip.end_frame == index + 1:
                self._clips.popleft()
                clip.done.set()
            return frame
        offset = index * FRAME_SAMPLES % len(self._idle)
        return self._idle[offset : offset + FRAME_SAMPLES]

    @property
    def busy(self) -> bool:
        """Whether a clip is playing or queued."""
        return bool(self._clips)


@dataclass(slots=True)
class Segment:
    """One stretch of agent speech; times in trial ms, samples on the agent recording."""

    start_ms: float
    start_sample: int
    last_ms: float
    """End of the latest speech heard so far (the end, once closed)."""
    last_sample: int
    closed: bool = False


class AgentSpeech:
    """Segments the agent's frames into speech, closing a segment after ``hangover_ms`` of quiet.

    Edges are sample-accurate: within the first and last speech frames, the segment starts at
    the first sample above the speech amplitude and ends after the last one, so the precision
    is far below the 20 ms frame size. ``on_open`` / ``on_close`` are called synchronously.
    """

    def __init__(
        self,
        hangover_ms: float,
        on_open: Callable[[Segment], None],
        on_close: Callable[[Segment], None],
    ) -> None:
        self._hangover_ms = hangover_ms
        self._on_open = on_open
        self._on_close = on_close
        self.open: Segment | None = None
        self.last_end_ms: float | None = None
        """End of the most recent closed segment."""

    def frame(self, start_ms: float, start_sample: int, pcm: Pcm16, speech: bool) -> None:
        """Account for one received frame starting at ``start_ms`` / ``start_sample``."""
        if not speech:
            self.quiet_until(start_ms)
            return
        first, last = speech_edges(pcm) or (0, len(pcm))
        if self.open is None:
            self.open = Segment(
                start_ms=start_ms + first / SAMPLES_PER_MS,
                start_sample=start_sample + first,
                last_ms=start_ms,
                last_sample=start_sample,
            )
            self._on_open(self.open)
        self.open.last_ms = start_ms + last / SAMPLES_PER_MS
        self.open.last_sample = start_sample + last

    def quiet_until(self, now_ms: float) -> None:
        """No speech up to ``now_ms``: close the open segment once the hangover has passed."""
        if self.open is not None and now_ms - self.open.last_ms >= self._hangover_ms:
            self.close()

    def close(self) -> None:
        """End the open segment at its last speech sample (also used when the call ends)."""
        if self.open is None:
            return
        segment, self.open = self.open, None
        segment.closed = True
        self.last_end_ms = segment.last_ms
        self._on_close(segment)

    def speaking_at(self, now_ms: float, tolerance_ms: float) -> bool:
        """Whether speech was heard within ``tolerance_ms`` before ``now_ms``."""
        return self.open is not None and now_ms - self.open.last_ms <= tolerance_ms
