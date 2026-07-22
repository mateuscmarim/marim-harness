import anyio
import pytest

from marim_harness.server.bus import EventBus

pytestmark = pytest.mark.anyio


async def test_publish_stamps_monotonic_seq_and_ts():
    bus = EventBus()
    a = bus.publish("turn.started", {"turn_id": "t1"})
    b = bus.publish("text.delta", {"text": "hi"})
    assert (a.seq, b.seq) == (1, 2)
    assert a.ts  # ISO timestamp present
    assert bus.last_seq == 2


async def test_subscriber_receives_backlog_then_live():
    bus = EventBus()
    bus.publish("a", {})
    bus.publish("b", {})
    sub = bus.attach(after_seq=0)
    assert (await sub.next_event()).type == "a"
    assert (await sub.next_event()).type == "b"
    bus.publish("c", {})
    assert (await sub.next_event()).type == "c"
    sub.close()
    assert bus.subscriber_count == 0


async def test_attach_after_seq_skips_already_seen():
    bus = EventBus()
    bus.publish("a", {})
    bus.publish("b", {})
    sub = bus.attach(after_seq=1)
    assert (await sub.next_event()).type == "b"
    sub.close()


async def test_gap_event_when_resume_point_fell_off_ring():
    bus = EventBus(ring_size=2)
    for name in ("a", "b", "c", "d"):  # ring now holds only c(3), d(4)
        bus.publish(name, {})
    sub = bus.attach(after_seq=1)  # asks for 2..: 2 is gone
    first = await sub.next_event()
    assert first.type == "stream.gap"
    assert first.data == {"resync": "history"}
    assert (await sub.next_event()).type == "c"
    assert (await sub.next_event()).type == "d"
    sub.close()


async def test_next_event_timeout_returns_none():
    bus = EventBus()
    sub = bus.attach()
    with anyio.fail_after(2):
        assert await sub.next_event(timeout=0.01) is None
    sub.close()


async def test_history_seq_starts_at_zero():
    bus = EventBus()
    assert bus.history_seq == 0


async def test_history_seq_advances_only_on_persisted_boundaries():
    # Deltas, turn.started, asks, and status never move the watermark: none of
    # them change the persisted message history. Only turn.finished / turn.error
    # / compaction.finished do, and each is published after its persist().
    bus = EventBus()
    bus.publish("turn.started", {"turn_id": "t1"})  # seq 1
    bus.publish("text.delta", {"text": "hi"})        # seq 2
    assert bus.history_seq == 0
    bus.publish("turn.finished", {"turn_id": "t1"})  # seq 3 -> persisted
    assert bus.history_seq == 3
    bus.publish("ask.pending", {"id": "a1"})         # seq 4
    bus.publish("session.status", {"status": "idle"})  # seq 5
    assert bus.history_seq == 3
    bus.publish("compaction.finished", {})           # seq 6 -> persisted
    assert bus.history_seq == 6
    bus.publish("turn.error", {"turn_id": "t2"})     # seq 7 -> rolled-back baseline on disk
    assert bus.history_seq == 7
