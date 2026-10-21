"""The trial's event log: one ``EventSink`` shared by the world, the driver and the harness."""

from hecklr_schemas import (
    Actor,
    Clock,
    Event,
    EventId,
    EventPayload,
    TrialId,
    new_id,
)


class TrialEventSink:
    """Append-only, in-memory event log of one trial attempt, stamped by the harness clock.

    Events are held in memory and written to storage once the trial ends, so a retried attempt
    never leaves a partial log behind. ``seq`` is the append position.
    """

    def __init__(self, trial_id: TrialId, clock: Clock) -> None:
        self._trial_id = trial_id
        self._clock = clock
        self._events: list[Event] = []

    @property
    def events(self) -> tuple[Event, ...]:
        """Every event emitted so far, in append order."""
        return tuple(self._events)

    def now_ms(self) -> int:
        """Milliseconds since the trial attempt started."""
        return self._clock.now_ms()

    def emit(self, actor: Actor, payload: EventPayload, *, t_ms: int | None = None) -> Event:
        """Append an event at ``t_ms`` (default: now) and return it."""
        event = Event(
            id=EventId(new_id()),
            trial_id=self._trial_id,
            seq=len(self._events),
            t_ms=self.now_ms() if t_ms is None else t_ms,
            actor=actor,
            payload=payload,
        )
        self._events.append(event)
        return event
