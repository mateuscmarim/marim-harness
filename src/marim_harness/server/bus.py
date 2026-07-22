"""Per-session event bus: monotonic sequence numbers, a bounded replay ring,
and queue-based subscriptions.

The bus outlives any single SessionHost (the supervisor keys buses separately
from hosts) so an SSE client can attach, disconnect, and resume with
Last-Event-ID across host evictions within the daemon's lifetime.

Subscription is a queue handle, not an async generator: the SSE writer wraps
each read in ``asyncio.wait_for`` to emit keepalive comments, and cancelling a
suspended async-generator ``__anext__`` would kill the generator — a plain
``Queue.get`` just retries."""

import asyncio
from collections import deque
from datetime import datetime, timezone

from .schema import Event


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Subscription:
    """One attached consumer: a snapshot backlog (replayed events, possibly
    prefixed by a synthetic ``stream.gap``) then a live queue."""

    def __init__(
        self, bus: "EventBus", queue: "asyncio.Queue[Event]", backlog: list[Event]
    ) -> None:
        self._bus = bus
        self._queue = queue
        self._backlog = backlog

    async def next_event(self, timeout: float | None = None) -> Event | None:
        """The next event, or None when ``timeout`` elapses (heartbeat tick)."""
        if self._backlog:
            return self._backlog.pop(0)
        if timeout is None:
            return await self._queue.get()
        try:
            return await asyncio.wait_for(self._queue.get(), timeout)
        except asyncio.TimeoutError:
            return None

    def close(self) -> None:
        self._bus._detach(self._queue)


class EventBus:
    # Events whose publication means the on-disk message history now reflects
    # them: a turn's messages are persisted inside run_turn *before* the host
    # publishes turn.finished; a failed turn rolls back to its persisted
    # baseline before turn.error; compaction persists the compacted history
    # before compaction.finished. So the seq of the latest such event is a safe
    # watermark for "everything <= this seq is durably on disk" — see
    # history_seq.
    _PERSISTED_BOUNDARIES = frozenset(
        {"turn.finished", "turn.error", "compaction.finished"}
    )

    def __init__(self, ring_size: int = 1000) -> None:
        self._ring: deque[Event] = deque(maxlen=ring_size)
        self._seq = 0
        self._history_seq = 0
        self._queues: set[asyncio.Queue[Event]] = set()

    @property
    def last_seq(self) -> int:
        return self._seq

    @property
    def history_seq(self) -> int:
        """The seq up to which the persisted message history is consistent.

        Advances only on ``_PERSISTED_BOUNDARIES``, each published after its
        persist() completes, so a /history read reporting this value is served
        from a file that already contains every event <= it. A live event with
        a larger seq is genuinely absent from that snapshot and safe to render
        without duplicating the history copy."""
        return self._history_seq

    @property
    def subscriber_count(self) -> int:
        return len(self._queues)

    def publish(self, type: str, data: dict) -> Event:
        self._seq += 1
        if type in self._PERSISTED_BOUNDARIES:
            self._history_seq = self._seq
        event = Event(seq=self._seq, ts=_now(), type=type, data=data)
        self._ring.append(event)
        for queue in self._queues:
            queue.put_nowait(event)
        return event

    def attach(self, after_seq: int | None = None) -> Subscription:
        """Attach a consumer. With ``after_seq``, replay ring events newer than
        it; when the resume point has fallen off the ring, prefix a synthetic
        ``stream.gap`` telling the client to re-sync via the history endpoint.

        No await between registering the queue and snapshotting the backlog, so
        an event is never both replayed and queued (single event loop)."""
        queue: asyncio.Queue[Event] = asyncio.Queue()
        self._queues.add(queue)
        backlog: list[Event] = []
        if after_seq is not None:
            replayed = [e for e in self._ring if e.seq > after_seq]
            oldest_held = self._ring[0].seq if self._ring else self._seq + 1
            if after_seq + 1 < oldest_held:
                gap_seq = replayed[0].seq - 1 if replayed else self._seq
                backlog.append(
                    Event(
                        seq=gap_seq,
                        ts=_now(),
                        type="stream.gap",
                        data={"resync": "history"},
                    )
                )
            backlog.extend(replayed)
        return Subscription(self, queue, backlog)

    def _detach(self, queue: "asyncio.Queue[Event]") -> None:
        self._queues.discard(queue)
