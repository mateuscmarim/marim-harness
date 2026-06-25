"""Tests for the turn-spawn race latch (``_turn_starting``) and the per-turn
pruning of completed tool-widget entries in the stream renderer."""

from __future__ import annotations

from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


# ---------------------------------------------------------------------------
# Turn-spawn race latch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_turn_starting_latch_makes_turn_busy_before_worker_exists(
    tmp_path: Path,
):
    """``turn_busy`` must be true the instant a turn starts, even before the
    exclusive worker has been created — otherwise a concurrent submit slips
    through and spawns a duplicate exclusive worker."""
    app = _app(tmp_path)
    async with app.run_test():
        assert app.turn_busy is False

        # Simulate being inside _start_turn after the latch is set but before the
        # worker is created (the exact window the latch guards).
        app._turn_starting = True
        assert app.turn_busy is True
        assert app._turn_worker is None  # worker not yet created in this window
        app._turn_starting = False
        assert app.turn_busy is False


@pytest.mark.anyio
async def test_concurrent_submit_during_start_gap_enqueues_not_duplicate(
    tmp_path: Path,
):
    """A submit landing while a turn is mid-spawn (latch set, worker not yet
    created) must be enqueued rather than starting a second exclusive worker."""
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    started: list[str] = []

    async def _fake_start_turn(text, attachments=None):
        started.append(text)
        # Mimic the real latch ordering: turn becomes busy via the worker.
        app._turn_worker = object()

    async with app.run_test():
        app._start_turn = _fake_start_turn  # type: ignore[assignment]

        # First submit: no turn running -> starts a turn.
        await app.on_prompt_input_submitted(PromptInput.Submitted("first", []))
        assert started == ["first"]

        # Now simulate the start-up gap precisely: worker not yet set, latch on.
        app._turn_worker = None
        app._turn_starting = True
        await app.on_prompt_input_submitted(PromptInput.Submitted("second", []))
        # Second did NOT start a turn; it was enqueued instead.
        assert started == ["first"]
        assert any(m.text == "second" for m in app._queue.items)


@pytest.mark.anyio
async def test_start_turn_clears_latch_on_success(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test():
        # The worker is created before the latch drops, so capture it right after
        # _start_turn returns (the fast TestModel turn may already have finished and
        # nulled _turn_worker by the time we check, so don't await a pause first).
        await app._start_turn("hello")
        # Latch cleared on the success path; the worker (created inside) carries the
        # busy flag from here on.
        assert app._turn_starting is False
        assert app._turn_worker is not None


@pytest.mark.anyio
async def test_start_turn_clears_latch_on_error(tmp_path: Path):
    """If the mount inside _start_turn raises, the latch must still drop, or the
    UI wedges (turn_busy stuck true with no worker)."""
    app = _app(tmp_path)
    async with app.run_test():
        def _boom(*a, **k):
            raise RuntimeError("no log")

        app.query_one = _boom  # type: ignore[assignment]
        with pytest.raises(RuntimeError):
            await app._start_turn("hello")
        assert app._turn_starting is False


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
    """The Ctrl+X viewer reads ``subagents`` directly; pruning the tracking dict
    must not touch that list."""
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
