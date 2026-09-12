"""``TurnTracker``: the TUI's wire-derived "is a turn running?" (phase 3b).

Pure unit tests — the folding of the submit latch, the current turn and the
host's status into one busy answer, and the transition an ``on_status`` call
reports on that folded answer."""

from marim_harness.interfaces.tui.turn_state import Transition, TurnTracker


def test_fresh_tracker_is_idle():
    t = TurnTracker()
    assert t.busy is False
    assert t.status == "idle"


def test_submit_latch_is_busy_until_its_own_turn_started():
    t = TurnTracker()
    t.note_submitted("t1")
    assert t.busy is True
    # Another turn's start (queued earlier, or another client's) does not
    # release a latch that belongs to a turn that has not started yet.
    t.on_started("t0")
    assert t.submitted == "t1" and t.current == "t0"
    assert t.busy is True
    t.on_started("t1")
    assert t.submitted is None and t.current == "t1"
    assert t.busy is True  # the turn itself is running now


def test_status_running_is_busy_without_a_latch_or_a_start():
    """A turn started by another client reaches the tracker through status
    alone — same code path, same answer."""
    t = TurnTracker()
    assert t.on_status("running") is Transition.BECAME_BUSY
    assert t.busy is True
    assert t.on_status("idle") is Transition.BECAME_IDLE
    assert t.busy is False


def test_waiting_ask_keeps_the_current_turn():
    t = TurnTracker()
    t.on_started("t1")
    assert t.on_status("waiting_ask") is Transition.NONE  # was busy, still busy
    assert t.current == "t1"
    assert t.on_status("running") is Transition.NONE
    assert t.current == "t1"


def test_idle_clears_the_current_turn_and_reports_the_edge():
    t = TurnTracker()
    t.on_started("t1")
    t.on_status("running")
    assert t.on_status("idle") is Transition.BECAME_IDLE
    assert t.current is None and t.busy is False
    # A second idle is not a second edge.
    assert t.on_status("idle") is Transition.NONE


def test_idle_while_a_submit_is_latched_is_not_an_idle_edge():
    """The previous turn's idle lands while the next one is already on its way:
    the folded answer never went idle, so the after-turn hand-off must wait."""
    t = TurnTracker()
    t.on_started("t1")
    t.note_submitted("t2")  # queued behind t1 (drained, or a second Enter)
    assert t.on_status("idle") is Transition.NONE
    assert t.busy is True
    assert t.current is None and t.submitted == "t2"
    t.on_started("t2")
    assert t.on_status("idle") is Transition.BECAME_IDLE
    assert t.busy is False


def test_finished_without_a_start_releases_the_latch():
    """An interrupt that lands before the turn's first step: the wire carries
    turn.finished + idle and no turn.started. The finish is folded as an
    implied start, so the answer stays busy until the idle that follows —
    which is then a real edge, reported from the one place edges come from."""
    t = TurnTracker()
    t.note_submitted("t1")
    t.on_finished("t0")  # some other turn's end does not touch it
    assert t.submitted == "t1" and t.busy is True
    t.on_finished("t1")
    assert t.submitted is None and t.current == "t1" and t.busy is True
    assert t.on_status("idle") is Transition.BECAME_IDLE
    assert t.busy is False


def test_finished_after_a_start_is_a_no_op():
    t = TurnTracker()
    t.note_submitted("t1")
    t.on_started("t1")
    t.on_finished("t1")
    assert t.current == "t1" and t.busy is True  # still the status's call
    assert t.on_status("idle") is Transition.BECAME_IDLE
