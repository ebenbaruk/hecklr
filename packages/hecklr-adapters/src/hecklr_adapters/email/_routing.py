"""Which customer addresses belong to which trial.

Agents answer the address they were written from, and a broken agent may drop every threading
header (fault F11), so replies are routed by recipient address, never by ``In-Reply-To``. The
driver writes each conversation from a fresh address, so an address names one conversation.
"""

import threading


class TrialAddresses:
    """Thread-safe map between trials and the customer addresses they wrote from."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._by_trial: dict[str, set[str]] = {}

    def add(self, trial_id: str, address: str) -> None:
        """Record that ``trial_id`` wrote from ``address``."""
        with self._lock:
            self._by_trial.setdefault(trial_id, set()).add(address.casefold())

    def of(self, trial_id: str) -> frozenset[str]:
        """The addresses ``trial_id`` wrote from so far (case-folded)."""
        with self._lock:
            return frozenset(self._by_trial.get(trial_id, ()))

    def trial_of(self, address: str) -> str | None:
        """The trial that wrote from ``address``, if any."""
        folded = address.casefold()
        with self._lock:
            return next((t for t, addrs in self._by_trial.items() if folded in addrs), None)

    def discard(self, trial_id: str) -> None:
        """Forget a finished trial."""
        with self._lock:
            self._by_trial.pop(trial_id, None)
