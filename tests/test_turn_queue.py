from marim_harness.interfaces.tui.queue import TurnQueue


def test_enqueue_appends_in_order_with_monotonic_ids():
    q = TurnQueue()
    q.enqueue("a")
    q.enqueue("b", [(b"img", "png")])
    assert [m.text for m in q.items] == ["a", "b"]
    assert [m.id for m in q.items] == ["1", "2"]
    assert q.items[1].attachments == [(b"img", "png")]


def test_prepend_inserts_at_front_and_keeps_seq_monotonic():
    q = TurnQueue()
    q.enqueue("first")
    q.prepend("jumped")
    assert [m.text for m in q.items] == ["jumped", "first"]
    # prepend still advances the sequence — ids never collide with enqueue's.
    assert q.items[0].id == "2"


def test_prepend_multiple_in_reversed_loop_preserves_original_order():
    # Mirrors _after_turn: leftover steers [s1, s2] re-inserted via
    # `for x in reversed(leftover): prepend(x)` must end up [s1, s2] at the front.
    q = TurnQueue()
    q.enqueue("queued")
    for text in reversed(["s1", "s2"]):
        q.prepend(text)
    assert [m.text for m in q.items] == ["s1", "s2", "queued"]


def test_pop_next_returns_and_removes_front():
    q = TurnQueue()
    q.enqueue("a")
    q.enqueue("b")
    item = q.pop_next()
    assert item.text == "a"
    assert [m.text for m in q.items] == ["b"]


def test_remove_drops_by_id_and_is_noop_for_absent():
    q = TurnQueue()
    q.enqueue("a")  # id "1"
    q.enqueue("b")  # id "2"
    q.remove("1")
    assert [m.text for m in q.items] == ["b"]
    q.remove("999")  # absent — no error, no change
    assert [m.text for m in q.items] == ["b"]


def test_take_pops_specific_id_and_returns_none_when_absent():
    q = TurnQueue()
    q.enqueue("a")  # id "1"
    q.enqueue("b")  # id "2"
    taken = q.take("2")
    assert taken is not None and taken.text == "b"
    assert [m.text for m in q.items] == ["a"]
    assert q.take("2") is None


def test_bool_reflects_emptiness_and_paused_defaults_false():
    q = TurnQueue()
    assert not q
    assert q.paused is False
    q.enqueue("a")
    assert q
