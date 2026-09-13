"""Tests for the submitted-turn latch (``TurnTracker.submitted`` — the window
between ``host.submit()`` and that turn's ``turn.started``) and the per-turn
pruning of completed tool-widget entries in the stream renderer."""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest

from tests.conftest import _make_deps


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


# ---------------------------------------------------------------------------
# Submit latch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_submit_latch_makes_turn_busy_before_turn_started_arrives(tmp_path: Path):
    """``turn_busy`` must be true the instant a turn is submitted — before the
    host's worker has picked it up and the pump has delivered turn.started —
    otherwise a concurrent submit slips through as a duplicate."""
    from marim_harness.interfaces.tui.turn_state import Transition

    app = _app(tmp_path)
    async with app.run_test():
        assert app.turn_busy is False
        app.host.submit = lambda *a, **k: "t1"  # type: ignore[method-assign]  # never runs
        await app.start_turn("hello")
        assert app.turn_busy is True
        assert app.turns.submitted == "t1"
        # turn.started for THAT id releases the latch; the turn keeps busy...
        app.turns.on_started("t1")
        assert app.turns.submitted is None and app.turn_busy is True
        # ...and only the host's idle status frees it.
        assert app.turns.on_status("running") is Transition.NONE
        assert app.turn_busy is True
        assert app.turns.on_status("idle") is Transition.BECAME_IDLE
        assert app.turn_busy is False


@pytest.mark.anyio
async def test_concurrent_submit_during_start_gap_enqueues_not_duplicate(tmp_path: Path):
    """A submit landing while a turn is latched (submitted, turn.started not
    yet delivered) must be enqueued rather than submitted a second time."""
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    submitted: list[str] = []

    def fake_submit(prompt, attachments=None, *, trigger="user"):
        submitted.append(prompt)
        return f"t{len(submitted)}"

    async with app.run_test():
        app.host.submit = fake_submit  # type: ignore[method-assign]

        # First submit: no turn running -> submitted.
        await app.on_prompt_input_submitted(PromptInput.Submitted("first", []))
        assert submitted == ["first"]

        # Still inside the gap: no turn.started has arrived for "first".
        await app.on_prompt_input_submitted(PromptInput.Submitted("second", []))
        assert submitted == ["first"]  # NOT submitted again...
        assert any(m.text == "second" for m in app.queue.items)  # ...enqueued instead


@pytest.mark.anyio
async def test_real_turn_releases_the_latch_on_the_idle_edge(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test():
        await app.start_turn("hello")
        assert app.turn_busy is True  # latched before the host has reported anything
        # Drain the turn while the screen (and #log) is still mounted, so its
        # rendering never lands after run_test's teardown removed the log.
        await asyncio.wait_for(app.turns_idle.wait(), 10)
        assert app.turns.submitted is None
        assert app.turn_busy is False


@pytest.mark.anyio
async def test_full_host_queue_restages_the_prompt_and_pauses(tmp_path: Path):
    """TurnQueueFull: the user's text is kept at the FRONT of the TUI queue,
    the queue pauses so the user picks when to retry, and a notice says so.
    Nothing was submitted, so nothing is latched."""
    from marim_harness.interfaces.tui.widgets import NoticeMessage
    from marim_harness.server.host import TurnQueueFull

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        def full(*a, **k):
            raise TurnQueueFull()

        app.host.submit = full  # type: ignore[method-assign]
        app.queue.enqueue("later")
        await app.start_turn("now")
        await pilot.pause()
        assert [m.text for m in app.queue.items] == ["now", "later"]
        assert app.queue.paused is True
        assert app.turn_busy is False
        assert any("queue is full" in str(n.render()) for n in app.query(NoticeMessage))


@pytest.mark.anyio
async def test_closed_host_drops_the_submit_silently(tmp_path: Path):
    """HostClosed only happens during teardown: the prompt is dropped (logged),
    nothing is latched, nothing is staged."""
    from marim_harness.interfaces.tui.widgets import NoticeMessage
    from marim_harness.server.host import HostClosed

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        def closed(*a, **k):
            raise HostClosed()

        app.host.submit = closed  # type: ignore[method-assign]
        await app.start_turn("too late")
        await pilot.pause()
        assert app.turn_busy is False
        assert app.queue.items == []
        assert not any("queue" in str(n.render()) for n in app.query(NoticeMessage))


# ---------------------------------------------------------------------------
# Compaction-worker concurrency guard (I1): the reset/new/switch flows and turn
# submission refuse while the /compact worker is mid-summarize.
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_compact_busy_blocks_reset_new_and_switch(tmp_path: Path):
    """While ``compact_busy`` is latched, /clear, /new, and /switch refuse with a
    notice rather than rebinding the session store under the compact worker."""
    app = _app(tmp_path)
    posted: list[str] = []

    async def _capture(msg: str) -> None:
        posted.append(msg)

    reached: list[str] = []
    async with app.run_test():
        app.post_system = _capture  # type: ignore[assignment]
        # Trip guards if a flow slips past the compact-busy check.
        app.session.reset_conversation = lambda *a, **k: reached.append("reset")  # type: ignore[assignment]
        app.session.start_new_session = lambda *a, **k: reached.append("new")  # type: ignore[assignment]
        app.session.switch_to_session_id = lambda *a, **k: reached.append("switch")  # type: ignore[assignment]
        app.compact_busy = True

        await app.reset_conversation()
        await app.start_new_session("x")
        await app.switch_to_session_id("whatever")

    assert reached == []  # none of the session flows ran
    assert len(posted) == 3
    assert all("Compaction in progress" in m for m in posted)


@pytest.mark.anyio
async def test_compact_busy_refuses_turn_submission_without_losing_it_silently(
    tmp_path: Path,
):
    """A turn submitted mid-compaction is refused with a notice — NOT started
    (which would race the summarize) and NOT silently enqueued."""
    app = _app(tmp_path)
    started: list[str] = []

    async def _fake_start_turn(text, attachments=None):
        started.append(text)

    async with app.run_test():
        app.start_turn = _fake_start_turn  # type: ignore[assignment]
        app.compact_busy = True
        await app._route_submission("hello", None)

        assert started == []  # not started
        assert not app.queue.items  # not silently enqueued either


# ---------------------------------------------------------------------------
# Per-turn pruning of completed tool-widget entries
# ---------------------------------------------------------------------------


def test_prune_completed_drops_finished_keeps_in_flight():
    from marim_harness.interfaces.tui.stream_render import StreamRenderer

    class _W:
        def __init__(self, status):
            self.status = status

    r = StreamRenderer(app=None)
    r.tool_widgets = {
        "done1": _W("done"),
        "failed1": _W("failed"),
        "denied1": _W("denied"),
        "live1": _W("pending"),  # in-flight: must survive
    }
    r.prune_completed()
    assert set(r.tool_widgets) == {"live1"}
    assert r.tool_widgets["live1"].status == "pending"


def test_prune_completed_preserves_subagents_list_for_viewer():
    """The Ctrl+X sub-agents screen reads ``subagents`` directly; pruning the
    tracking dict must not touch that list."""
    from marim_harness.interfaces.tui.stream_render import StreamRenderer

    class _Sub:
        def __init__(self, status):
            self.status = status

    r = StreamRenderer(app=None)
    finished = _Sub("done")
    r.subagents = [finished]
    r.tool_widgets = {"sid": finished}
    r.prune_completed()
    # Dropped from the dict (finished) ...
    assert "sid" not in r.tool_widgets
    # ... but still available to the viewer.
    assert r.subagents == [finished]


def test_prune_completed_empty_is_noop():
    from marim_harness.interfaces.tui.stream_render import StreamRenderer

    r = StreamRenderer(app=None)
    r.prune_completed()
    assert r.tool_widgets == {}


@pytest.mark.anyio
async def test_escape_before_turn_started_still_releases_the_latch(tmp_path: Path):
    """Esc can cancel the host's turn task in the one loop iteration between
    the worker creating it and ``_turn_body`` publishing turn.started. The
    turn then ends with only ``turn.finished {interrupted}`` + ``session.status
    idle`` and NO turn.started — the latch must be released by the finish, or
    the TUI stays busy forever (review-bot finding on PR #118)."""
    app = _app(tmp_path)
    async with app.run_test():
        await app.start_turn("hi")
        # One iteration: the worker dequeues and creates the task, but the task's
        # first step is queued behind us, so the cancel lands before turn.started.
        await asyncio.sleep(0)
        assert app.host._turn_task is not None
        await app.action_cancel_turn()
        await asyncio.wait_for(app.turns_idle.wait(), 10)
        assert app.turn_busy is False
        assert app.turns.submitted is None
        assert app.host.status == "idle"
        assert not app.query("UserMessage")  # the turn never started
