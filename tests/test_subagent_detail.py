import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.subagents.pane import (
    SubAgentDetailHost,
    SubAgentPane,
    _short_model,
    pane_id,
)


def test_pane_id_sanitizes_tool_call_id():
    pid = pane_id("call/abc.123:x")
    assert pid.startswith("sap-")
    assert all(c.isalnum() or c in "-_" for c in pid)


def test_short_model_takes_last_path_segment():
    assert _short_model("openrouter/openrouter/owl-alpha") == "owl-alpha"
    assert _short_model("openrouter/anthropic/claude-sonnet-4-6") == "claude-sonnet-4-6"
    assert _short_model("owl-alpha") == "owl-alpha"
    assert _short_model("") == ""


@pytest.mark.anyio
async def test_pane_header_is_title_with_model_subtitle():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane(
            "c1", "explore", "openrouter/openrouter/owl-alpha", "Architecture review"
        )
        await pilot.pause()
        # Line 1 is the description/title (the headline).
        assert "Architecture review" in str(pane._header.render())
        # Line 2 is the muted type · model subtitle, with a shortened model name.
        subhead = str(pane._subhead.render())
        assert "explore" in subhead
        assert "owl-alpha" in subhead  # short model name
        assert "openrouter/openrouter" not in subhead  # not the routed label
        assert pane._subhead.display is True


@pytest.mark.anyio
async def test_pane_subtitle_hidden_and_header_falls_back_when_no_title():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "explore", "owl")  # no title
        await pilot.pause()
        assert pane._subhead.display is False
        # With no title the header falls back to the type · model context.
        assert "explore" in str(pane._header.render())


@pytest.mark.anyio
async def test_task_disclosure_collapsed_by_default_then_expands():
    """The full task is hidden behind a collapsed '▸ task' line; toggling reveals
    the full prompt and flips the marker, and toggling again collapses it."""
    full = (
        "Read the plan file and relevant source files before writing any tests "
        "or moving code. Port the actionable-failure tests, then extract "
        "turn_controller.py, then update agent.py."
    )
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "claude-general", "sonnet", "Task 2", task=full)
        await pilot.pause()
        # Collapsed by default: toggle visible with a ▸ marker, body hidden.
        assert pane._task_toggle.display is True
        assert "▸" in str(pane._task_toggle.render())
        assert pane._task_body.display is False

        pane.toggle_task()
        await pilot.pause()
        # Expanded: ▾ marker, full task shown verbatim.
        assert "▾" in str(pane._task_toggle.render())
        assert pane._task_body.display is True
        assert full in str(pane._task_body.render())

        pane.toggle_task()
        await pilot.pause()
        assert pane._task_body.display is False
        assert "▸" in str(pane._task_toggle.render())


@pytest.mark.anyio
async def test_task_disclosure_hidden_when_no_task():
    """No task → no disclosure chrome at all (the line stays hidden)."""
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "explore", "owl", "Architecture review")
        await pilot.pause()
        assert pane._task_toggle.display is False
        # Toggling a task-less pane is a no-op, not a crash.
        pane.toggle_task()
        await pilot.pause()
        assert pane._task_body.display is False


@pytest.mark.anyio
async def test_task_toggle_click_expands():
    """Clicking the toggle line expands the task (the pane's click idiom)."""
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "explore", "owl", "Title", task="do the thing")
        await pilot.pause()
        assert pane._task_body.display is False
        pane.on_click(_FakeClick(pane._task_toggle))
        await pilot.pause()
        assert pane._task_body.display is True


class _FakeClick:
    """Minimal stand-in for a Textual Click event carrying the clicked widget."""

    def __init__(self, widget) -> None:
        self.widget = widget


@pytest.mark.anyio
async def test_set_model_replaces_subtitle_model():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        # Created with the harness fallback model (wrong for a claude-cli spawn).
        pane = host.add_pane("c1", "claude-general", "owl-alpha", "Explore layout")
        await pilot.pause()
        assert "owl-alpha" in str(pane._subhead.render())
        # The CLI reports its real model mid-stream.
        pane.set_model("claude-opus-4-8")
        await pilot.pause()
        subhead = str(pane._subhead.render())
        assert "claude-opus-4-8" in subhead
        assert "owl-alpha" not in subhead
        assert "claude-general" in subhead  # the type prefix is preserved


@pytest.mark.anyio
async def test_set_model_updates_headline_when_no_title():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("c1", "claude-general", "owl-alpha")  # no title
        await pilot.pause()
        pane.set_model("claude-opus-4-8")
        await pilot.pause()
        # With no title, the headline shows the type · model context — update it too.
        assert "claude-opus-4-8" in str(pane._header.render())
        assert "owl-alpha" not in str(pane._header.render())


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
async def test_pane_streams_mounted_children():
    app = _Host()
    async with app.run_test() as pilot:
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("call_1", "research", "sonnet")
        await pane.add(Static("hello transcript"))
        pane.set_usage_line("in 1.0k · out 0.2k · $0.01")
        await pilot.pause()
        assert len(pane.query(Static)) >= 2  # body header + the mounted child
