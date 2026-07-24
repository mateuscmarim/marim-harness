"""Unit tests for the shared WakeDriver orchestrator. Pure — the predicates and
the enqueue effect are injected, so no server and no Textual are involved."""

from marim_harness.runtime.wake import WakeController
from marim_harness.runtime.wake_driver import WakeDriver


def _driver(**over):
    """A driver whose predicates default to 'ready to wake' and whose enqueue
    appends to a list, so a test can flip one predicate and assert the effect."""
    fired: list[int] = []
    cfg = dict(
        is_enabled=lambda: True,
        turn_busy=lambda: False,
        has_finished_pending=lambda: True,
        all_jobs_settled=lambda: True,
    )
    cfg.update(over)
    driver = WakeDriver(
        WakeController(depth_cap=3),
        enqueue_digest_turn=lambda: fired.append(1),
        **cfg,
    )
    return driver, fired


def test_maybe_wake_fires_enqueue_when_ready():
    driver, fired = _driver()
    assert driver.maybe_wake() is True
    assert fired == [1]


def test_maybe_wake_suppressed_when_disabled():
    driver, fired = _driver(is_enabled=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_when_turn_busy():
    driver, fired = _driver(turn_busy=lambda: True)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_when_a_job_still_running():
    driver, fired = _driver(all_jobs_settled=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_without_pending_digest():
    driver, fired = _driver(has_finished_pending=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_depth_cap_bounds_the_chain():
    driver, fired = _driver()
    assert driver.maybe_wake() is True   # depth 0 -> 1
    assert driver.maybe_wake() is True   # depth 1 -> 2
    assert driver.maybe_wake() is True   # depth 2 -> 3
    assert driver.maybe_wake() is False  # depth 3 == cap -> capped
    assert fired == [1, 1, 1]


def test_note_user_turn_resets_the_chain():
    driver, fired = _driver()
    for _ in range(3):
        driver.maybe_wake()
    assert driver.maybe_wake() is False  # at cap
    driver.note_user_turn()
    assert driver.maybe_wake() is True   # chain reset -> wakes again
