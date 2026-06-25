import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.subagent_detail import (
    SubAgentDetailHost,
    SubAgentPane,
    pane_id,
)


def test_pane_id_sanitizes_tool_call_id():
    pid = pane_id("call/abc.123:x")
    assert pid.startswith("sap-")
    assert all(c.isalnum() or c in "-_" for c in pid)


class _Host(App):
    def compose(self) -> ComposeResult:
        yield SubAgentDetailHost()


@pytest.mark.anyio
async def test_host_adds_and_switches_panes():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        p1 = host.add_pane("call_1", "research", "sonnet")
        p2 = host.add_pane("call_2", "coding", "sonnet")
        await pilot.pause()
        assert isinstance(p1, SubAgentPane) and p1.stream_id == "call_1"
        assert host.pane("call_2") is p2
        host.show("call_1")
        await pilot.pause()
        assert host.current_sid() == "call_1"
        host.show("call_2")
        await pilot.pause()
        assert host.current_sid() == "call_2"


@pytest.mark.anyio
async def test_only_the_current_pane_is_visible():
    """Panes are mounted dynamically (one per spawn). ContentSwitcher only hides
    children present at compose time and only toggles the old/new pair on a switch,
    so a dynamically-mounted pane must be hidden on add — otherwise every pane
    renders stacked instead of one-at-a-time."""
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        p1 = host.add_pane("c1", "general", "owl")
        p2 = host.add_pane("c2", "general", "owl")
        p3 = host.add_pane("c3", "general", "owl")
        await pilot.pause()
        # Nothing selected yet → nothing visible (the screen is closed/blank).
        assert [p.display for p in (p1, p2, p3)] == [False, False, False]
        host.show("c2")
        await pilot.pause()
        # Exactly one pane visible; the others stay hidden.
        assert p2.display is True
        assert p1.display is False and p3.display is False
        # A pane added while another is current must not pop into view.
        p4 = host.add_pane("c4", "general", "owl")
        await pilot.pause()
        assert p4.display is False and p2.display is True


@pytest.mark.anyio
async def test_pane_streams_and_placeholder():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("call_1", "research", "sonnet")
        await pane.add(Static("hello transcript"))
        pane.set_usage_line("in 1.0k · out 0.2k · $0.01")
        await pilot.pause()
        assert len(pane.query(Static)) >= 2  # body header + the mounted child
        pane.placeholder()
        await pilot.pause()
        assert pane._placeholder.display is True
