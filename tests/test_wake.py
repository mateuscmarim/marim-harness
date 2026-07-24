"""Unit tests for the TUI autonomous-wake policy. Pure — no App, no Textual."""

from marim_harness.runtime.wake import WakeController

_READY = dict(enabled=True, turn_busy=False, has_finished_pending=True, all_jobs_settled=True)


def test_wakes_when_all_conditions_hold():
    assert WakeController(depth_cap=3).should_wake(**_READY) is True


def test_does_not_wake_when_disabled():
    assert WakeController(depth_cap=3).should_wake(**{**_READY, "enabled": False}) is False


def test_does_not_wake_while_a_turn_is_busy():
    assert WakeController(depth_cap=3).should_wake(**{**_READY, "turn_busy": True}) is False


def test_does_not_wake_without_a_finished_job():
    assert (
        WakeController(depth_cap=3).should_wake(**{**_READY, "has_finished_pending": False})
        is False
    )


def test_depth_cap_stops_further_wakes():
    wake = WakeController(depth_cap=2)
    assert wake.should_wake(**_READY) is True
    wake.record_auto_turn()
    assert wake.should_wake(**_READY) is True  # depth 1 < cap 2
    wake.record_auto_turn()
    assert wake.depth == 2
    assert wake.should_wake(**_READY) is False  # depth 2 == cap -> capped


def test_reset_clears_the_chain():
    wake = WakeController(depth_cap=1)
    wake.record_auto_turn()
    assert wake.should_wake(**_READY) is False  # at cap
    wake.reset()
    assert wake.depth == 0
    assert wake.should_wake(**_READY) is True


def test_should_wake_is_pure():
    wake = WakeController(depth_cap=3)
    wake.should_wake(**_READY)
    assert wake.depth == 0  # predicate never mutates the counter


def test_does_not_wake_while_a_job_is_still_running():
    wc = WakeController(depth_cap=3)
    # A finished job is pending, but another is still running → hold off.
    assert wc.should_wake(
        enabled=True, turn_busy=False,
        has_finished_pending=True, all_jobs_settled=False,
    ) is False


def test_wakes_once_all_jobs_settled():
    wc = WakeController(depth_cap=3)
    assert wc.should_wake(
        enabled=True, turn_busy=False,
        has_finished_pending=True, all_jobs_settled=True,
    ) is True
