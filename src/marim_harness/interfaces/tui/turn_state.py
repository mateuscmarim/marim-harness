"""The TUI's view of "is a turn running?", derived from the wire (phase 3b).

Before 3b the answer was local: a Textual worker object existed or not. With
turns driven through ``SessionHost.submit()`` and completed by bus events, the
truth lives on the host and reaches the app one pump hop later. Two sources are
folded into one answer:

- ``session.status`` — the host's own word (``idle`` | ``running`` |
  ``waiting_ask``). Authoritative, but it lags a ``submit()`` by however long
  the worker takes to pick the turn up and the pump takes to deliver
  ``turn.started``.
- the *submitted latch* — the turn id ``submit()`` handed back, held until its
  ``turn.started`` arrives. This closes the lag: a second Enter landing in that
  window must queue, not submit a duplicate, and a system command must refuse.

The two are never compared for consistency; the tracker is busy if *either*
says so, and idle only when both agree. A turn started by another client
(phase 4) shows up through ``turn.started`` + status alone, with no latch — the
same code path, the same answer.

Pure: no Textual, no asyncio, unit-tested directly.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class Transition(Enum):
    """What a ``session.status`` event changed about the busy answer."""

    NONE = "none"
    BECAME_IDLE = "became_idle"
    BECAME_BUSY = "became_busy"


@dataclass
class TurnTracker:
    submitted: str | None = None
    current: str | None = None
    status: str = "idle"

    @property
    def busy(self) -> bool:
        return self.submitted is not None or self.current is not None or self.status != "idle"

    def note_submitted(self, turn_id: str) -> None:
        """``submit()`` returned ``turn_id``; hold it until its turn.started."""
        self.submitted = turn_id

    def on_started(self, turn_id: str) -> None:
        """turn.started arrived. Releases the latch only when it is *this* turn:
        an earlier turn's late start (another client's, or a queued one) must
        not free a latch belonging to a turn that has not started yet."""
        self.current = turn_id
        if self.submitted == turn_id:
            self.submitted = None

    def on_status(self, status: str) -> Transition:
        """session.status arrived. ``current`` is cleared only on ``idle`` —
        ``waiting_ask`` is still the same turn. The transition is reported on
        the folded answer, so an ``idle`` that lands while a submit is latched
        is NOT an idle edge: the next turn is already on its way and the
        after-turn hand-off (queue drain, wake) must wait for *its* end."""
        was_busy = self.busy
        self.status = status
        if status == "idle":
            self.current = None
        now_busy = self.busy
        if was_busy and not now_busy:
            return Transition.BECAME_IDLE
        if now_busy and not was_busy:
            return Transition.BECAME_BUSY
        return Transition.NONE
