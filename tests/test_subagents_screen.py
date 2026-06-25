from pathlib import Path

import pytest
from textual.app import App, ComposeResult

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
        assert "2" in rendered  # total agents


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
