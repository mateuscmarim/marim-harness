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
