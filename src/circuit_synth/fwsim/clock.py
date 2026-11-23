"""Simulated time: a clock in microseconds with an event queue.

Every model in a simulation shares one Clock.  The firmware harness advances
it in step with the firmware's own notion of time.
"""

from __future__ import annotations

import heapq
import itertools


class Clock:
    """Simulated time in microseconds plus an event queue."""

    def __init__(self):
        self.now = 0.0
        self._events: list = []
        self._seq = itertools.count()

    def at(self, when: float, fn):
        heapq.heappush(self._events, (when, next(self._seq), fn))

    def after(self, delay: float, fn):
        self.at(self.now + delay, fn)

    def advance_to(self, when: float):
        while self._events and self._events[0][0] <= when:
            t, _, fn = heapq.heappop(self._events)
            self.now = max(self.now, t)
            fn()
        self.now = max(self.now, when)

    def advance(self, delta: float):
        self.advance_to(self.now + delta)

    def next_event(self) -> float | None:
        return self._events[0][0] if self._events else None
