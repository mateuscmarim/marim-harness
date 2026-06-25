from pathlib import Path

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.subagent_detail import SubAgentDetailHost
from marim_harness.interfaces.tui.widgets.subagent_stats import aggregate
from marim_harness.interfaces.tui.widgets.subagent_viewer import SubAgentList
from marim_harness.interfaces.tui.widgets.subagents_view import SubAgentSummary, SubAgentsView


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
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
        assert app.subagent_viewer_open is True
        assert view.display is True
        assert view.host.current_sid() == "call_1"


@pytest.mark.anyio
async def test_detached_spawn_shows_pane_placeholder(tmp_path):
    """A detached spawn streams nothing into its pane, so the pane shows the
    'no live transcript' placeholder rather than an empty transcript."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        w = r.mount_spawn_widget({"type": "research", "description": "bg work"})
        w.stream_id = "call_1"
        r.tool_widgets["call_1"] = w
        r.ensure_pane(w)
        await app.query_one("#log").mount(w)
        await pilot.pause()

        # A detach handoff keeps the card pending and marks the pane as detached.
        kept = r.note_detached_spawn(
            "Started detached sub-agent job-1, watching…", w, app.harness.deps.jobs
        )
        await pilot.pause()
        assert kept is True
        assert w.detached is True
        assert w.pane._placeholder.display is True


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
        app.refresh_subagents_view()
        assert app.subagent_viewer_open is False

        app.open_subagents_at("call_1")
        await pilot.pause()
        view = app.query_one(SubAgentsView)
        assert view.list.row_count == 1

        # A second spawn + refresh ticks the list to two rows live.
        w2 = r.mount_spawn_widget({"type": "coding", "description": "build it"})
        w2.stream_id = "call_2"
        r.tool_widgets["call_2"] = w2
        r.ensure_pane(w2)
        app.refresh_subagents_view()
        await pilot.pause()
        assert view.list.row_count == 2


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
        app.refresh_subagents_view()
        await pilot.pause()
        assert w.status == "done"
