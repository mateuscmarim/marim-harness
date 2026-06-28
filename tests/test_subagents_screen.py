from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.subagent_detail import SubAgentDetailHost
from marim_harness.interfaces.tui.widgets.subagent_stats import aggregate
from marim_harness.interfaces.tui.widgets.subagent_viewer import SubAgentList
from marim_harness.interfaces.tui.widgets.subagents_view import SubAgentSummary, SubAgentsView
from tests.conftest import _make_deps


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.deps import Deps
    from marim_harness.runtime.harness import Harness
    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


class _ListApp(App):
    def compose(self) -> ComposeResult:
        yield SubAgentList()
        yield SubAgentSummary()


@pytest.mark.anyio
async def test_list_rows_and_summary(monkeypatch):
    from tests.test_subagent_stats import FakeAgent  # reuse the stand-in

    agents = [
        FakeAgent(status="pending", agent_type="research", tool_count=2, tokens=100),
        FakeAgent(status="done", agent_type="coding", tokens=200, cost_text="$0.02"),
    ]
    app = _ListApp()
    async with app.run_test() as pilot:
        lst = app.query_one(SubAgentList)
        lst.refresh_rows(agents, selected=1)
        summ = app.query_one(SubAgentSummary)
        summ.refresh_totals(aggregate(agents, cost_of=lambda a: 0.0))
        await pilot.pause()
        assert lst.row_count == 2
        assert lst.selected_index() == 1
        # Every column has a FIXED width so the stat columns (tools/tokens/cost/dur)
        # stay visible and aligned instead of being pushed off the pane by a long
        # "{type} — title" cell (which DataTable truncates to the agent width).
        assert all(c.width for c in lst.columns.values())
        assert len(lst.columns) == 6
        # summary text mentions the running/done split and total agents
        rendered = str(summ.render())
        assert "2 sub-agents" in rendered  # total agents (not just "2", which matches "200 tokens")


@pytest.mark.anyio
async def test_view_refresh_paints_list_and_host():
    from tests.test_subagent_stats import FakeAgent

    class _ViewApp(App):
        def compose(self):
            yield SubAgentsView()

    agents = [FakeAgent(status="done", tokens=100)]
    app = _ViewApp()
    async with app.run_test() as pilot:
        view = app.query_one(SubAgentsView)
        view.repaint(agents, selected=0, cost_of=lambda a: 0.0)
        await pilot.pause()
        assert view.list.row_count == 1
        assert "1 sub-agents" in str(view.query_one(SubAgentSummary).render())


@pytest.mark.anyio
async def test_spawn_creates_pane_attached_to_card(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        host = SubAgentDetailHost()
        await app.mount(host)  # Task 8 mounts via SubAgentsView; here we mount one directly
        r.detail_host = host
        widget = r.mount_spawn_widget({"type": "research", "description": "map it"})
        widget.stream_id = "call_1"
        r.tool_widgets["call_1"] = widget
        pane = r.ensure_pane(widget)
        await pilot.pause()
        assert widget.pane is pane
        assert r.detail_host.pane("call_1") is pane


@pytest.mark.anyio
async def test_ctrl_x_opens_view_and_shows_selected_transcript(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        await pilot.press("ctrl+x")
        await pilot.pause()
        view = app.query_one("SubAgentsView")
        assert view.display is True
        assert app.query_one("#log").display is False
        assert view.host.current_sid() == "call_1"

        await pilot.press("escape")
        await pilot.pause()
        assert view.display is False
        assert app.query_one("#log").display is True


@pytest.mark.anyio
async def test_clicking_card_opens_screen_at_that_agent(tmp_path):
    """A click on a (non-failed) card jumps into the screen focused on it."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        w.on_click(None)  # click-to-open
        await pilot.pause()
        view = app.query_one(SubAgentsView)
        assert app.subagents.open is True
        assert view.display is True
        assert view.host.current_sid() == "call_1"


@pytest.mark.anyio
async def test_detached_spawn_streams_live_with_bg_marker(tmp_path):
    """Phase 2: a detached spawn is marked as a background run (bg marker + detached
    flag) and kept pending for settle, but streams live into its pane — no
    'no live transcript' placeholder."""
    from marim_harness.interfaces.tui.widgets.subagent_stats import row_cells

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "bg work"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        kept = r.note_detached_spawn(
            "Started detached sub-agent job-1, watching…", w, app.harness.deps.jobs
        )
        await pilot.pause()
        assert kept is True
        assert w.detached is True                     # marked as a background run
        assert w.pane._placeholder.display is False    # no placeholder — it streams
        assert "bg" in str(w._header.render())         # bg marker on the card
        assert row_cells(w)[1].startswith("bg · ")     # bg marker on the list row


@pytest.mark.anyio
async def test_stream_event_after_clear_is_a_noop(tmp_path):
    """A background job that streams after /clear (its card cleared from the log)
    must not crash — on_subagent_event no-ops when the parent card is absent."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        r.reset()  # simulate /clear: cards + tool_widgets cleared
        await r.on_subagent_event("ghost", object(), None)  # must not raise
        await pilot.pause()
        assert r.tool_widgets.get("ghost") is None


@pytest.mark.anyio
async def test_refresh_subagents_view_ticks_list_live_while_open(tmp_path):
    """While the screen is open, refresh_subagents_view repaints the list as cards'
    scalars change; it's a no-op when closed."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        # Closed: a no-op (no crash, screen stays hidden).
        app.subagents.refresh()
        assert app.subagents.open is False

        app.subagents.open_at("call_1")
        await pilot.pause()
        view = app.query_one(SubAgentsView)
        assert view.list.row_count == 1

        # A second spawn + refresh marks the list dirty; the flush tick repaints it
        # to two rows live (the repaint is coalesced onto the tick, not per event).
        w2 = r.mount_spawn_widget({"type": "coding", "description": "build it"})
        w2.stream_id = "call_2"
        r.tool_widgets["call_2"] = w2
        r.ensure_pane(w2)
        app.subagents.refresh()
        r.flush_streams()  # the tick drains the dirty repaint
        await pilot.pause()
        assert view.list.row_count == 2


@pytest.mark.anyio
async def test_streamed_events_coalesce_list_repaint_to_flush_tick(tmp_path):
    """Streamed sub-agent events must NOT repaint the list inline — a full
    DataTable rebuild per token, ×N streams, pins a core during a fan-out. Each
    event marks the screen dirty; the flush tick repaints it once per frame."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "explore", "description": "map"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        app.subagents.open_at("call_1")
        await pilot.pause()

        # Spy on the list rebuild after the initial open paint.
        lst = app.query_one(SubAgentList)
        n = {"c": 0}
        orig = lst.refresh_rows

        def spy(*a, **k):
            n["c"] += 1
            return orig(*a, **k)

        lst.refresh_rows = spy

        # Many per-event repaint requests do not rebuild the table; they only mark
        # it dirty.
        for _ in range(10):
            app.subagents.refresh()
        assert n["c"] == 0
        assert app.subagents.dirty is True

        # The flush tick repaints exactly once and clears the dirty flag.
        r.flush_streams()
        assert n["c"] == 1
        assert app.subagents.dirty is False

        # A tick with no new events does not repaint again.
        r.flush_streams()
        assert n["c"] == 1


@pytest.mark.anyio
async def test_subagent_usage_priced_once_per_flush_tick(tmp_path, monkeypatch):
    """Sub-agent usage must not be priced inline per delta — resolve_cost is a
    genai-prices table lookup and a fan-out emits many deltas per frame ×N agents.
    note_subagent_usage stashes; the flush tick prices each card at most once per
    frame, and skips a card whose token total hasn't moved since it was last priced."""
    from types import SimpleNamespace

    import marim_harness.interfaces.tui.stream_render as sr

    calls = {"n": 0}
    real = sr.resolve_cost

    def counting(usage, model):
        calls["n"] += 1
        return real(usage, model)

    monkeypatch.setattr(sr, "resolve_cost", counting)

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "explore", "description": "map"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await pilot.pause()

        usage = SimpleNamespace(
            total_tokens=100, input_tokens=80, output_tokens=20,
            cache_read_tokens=0, cache_write_tokens=0, details={},
        )
        # Many deltas in one frame stash only — no pricing yet.
        for _ in range(5):
            r.note_subagent_usage(w, usage)
        assert calls["n"] == 0

        # The flush tick prices it exactly once and the total lands on the card.
        r.flush_streams()
        assert calls["n"] == 1
        assert w.tokens == 100

        # A tick with no token movement does not re-price.
        r.note_subagent_usage(w, usage)
        r.flush_streams()
        assert calls["n"] == 1

        # A new token total reprices exactly once.
        r.note_subagent_usage(w, SimpleNamespace(
            total_tokens=250, input_tokens=200, output_tokens=50,
            cache_read_tokens=0, cache_write_tokens=0, details={},
        ))
        r.flush_streams()
        assert calls["n"] == 2
        assert w.tokens == 250


@pytest.mark.anyio
async def test_live_repaint_preserves_user_selection(tmp_path):
    """A live stats repaint must not snap the selection back to the first agent.

    Moving the list cursor updates the cursor synchronously but its RowHighlighted
    fires async; if a fan-out's per-frame repaint lands before that message updates
    subagent_index, the repaint must follow the cursor (the source of truth), not
    force the stale stored index. Regression for the 'selecting an agent jumps back
    to the first' bug."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        for i in range(4):
            w = r.mount_spawn_widget({"type": "explore", "description": f"agent {i}"})
            w.stream_id = f"c{i}"
            r.tool_widgets[f"c{i}"] = w
            r.ensure_pane(w)
            await app.query_one("#log").mount(w)
        await pilot.pause()
        app.subagents.open_at("c0")
        await pilot.pause()

        lst = app.query_one(SubAgentList)
        # Move the cursor (cursor updates now; its RowHighlighted is still queued)…
        lst.move_cursor(row=2)
        # …and a live event repaints the list before that message is processed.
        app.subagents.refresh()
        r.flush_streams()
        await pilot.pause()
        await pilot.pause()

        assert lst.cursor_row == 2
        assert app.subagents.index == 2
        assert app.query_one(SubAgentsView).host.current_sid() == "c2"


@pytest.mark.anyio
async def test_refresh_subagents_view_is_noop_when_closed(tmp_path):
    """When the screen is closed, a streamed event must not even mark it dirty, so
    streaming pays nothing for a hidden screen."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.subagents.open is False
        app.subagents.refresh()
        assert app.subagents.dirty is False


@pytest.mark.anyio
async def test_open_screen_fits_viewport_no_double_scrollbar(tmp_path):
    """The full-bleed view must fit the visible area: opening it (even with a
    transcript taller than the pane) must not make the app's root Screen scroll —
    only the transcript pane gets a scrollbar, never a second outer one. The list
    also fills the body height so its right-border divider runs to the bottom."""
    app = _app(tmp_path)
    async with app.run_test(size=(120, 30)) as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "explore", "description": "map"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        for i in range(60):  # overflow the pane so it (and only it) scrolls
            await w.pane.add(Static(f"line {i}"))
        await pilot.pause()
        app.subagents.open_at("call_1")
        await pilot.pause()
        # The root Screen must not show a scrollbar (no double scrollbar).
        assert app.screen.show_vertical_scrollbar is False
        # The transcript pane is the single scroll surface.
        assert w.pane.show_vertical_scrollbar is True
        # The list fills the body height, so its border-right divider is full-height.
        body = app.query_one("#subagents-body")
        assert app.query_one("#subagent-list").size.height == body.size.height


@pytest.mark.anyio
async def test_live_stream_then_open_shows_current_transcript(tmp_path: Path):
    """Content streamed into a pane while the sub-agents screen is CLOSED is
    present and current when the screen is opened with ctrl+x.

    Distinct from test_ctrl_x_opens_view_and_shows_selected_transcript (which
    only checks routing) and test_refresh_subagents_view_ticks_list_live_while_open
    (which only checks the list row count).  This test specifically asserts that
    widgets added to the pane *before* the screen opens are still queryable after
    opening, and that finish() updates the status while the screen is open.
    """
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        # Stream while the screen is CLOSED: content lands in the pane regardless.
        await w.pane.add(Static("first line"))
        w.note_tool("read_file", {"path": "a.py"})
        await pilot.pause()
        # Open: the list shows the agent with its live tool count; transcript is current.
        await pilot.press("ctrl+x")
        await pilot.pause()
        view = app.query_one(SubAgentsView)
        assert view.list.row_count == 1
        assert view.host.current_sid() == "call_1"
        assert len(w.pane.query(Static)) >= 2  # body header + streamed line
        # Finish updates the row glyph live while open.
        w.finish("ok", status="done")
        app.subagents.refresh()
        await pilot.pause()
        assert w.status == "done"


@pytest.mark.anyio
async def test_live_pane_is_marked_transcript_loaded(tmp_path: Path):
    """A pane created for a live (streaming) sub-agent via ensure_pane is fed by
    the live stream, so it must be marked transcript_loaded. Otherwise the
    resume-time lazy-load fires on open, reads a sidecar that a still-running
    agent hasn't written yet, and appends 'transcript unavailable for this
    resumed sub-agent' over the live transcript."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "map it"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        pane = r.ensure_pane(w)
        await pilot.pause()
        assert pane is not None
        assert pane.transcript_loaded is True


def test_repaint_before_children_mount_is_noop():
    """A live stream flush tick can call repaint() after SubAgentsView is created
    but before its compose children mount (observed as a NoMatches crash on a
    loaded 3.10 CI runner). repaint() must skip that tick, not raise."""
    view = SubAgentsView()
    # Bare instance, never mounted -> SubAgentSummary/SubAgentList aren't queryable.
    view.repaint([], lambda _s: 0.0)  # must not raise NoMatches
