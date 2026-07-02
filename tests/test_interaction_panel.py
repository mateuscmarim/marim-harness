"""The InteractionPanel base: future-resolution lifecycle, teardown on worker
cancel, and scroll-key forwarding to the transcript."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.interfaces.tui.interaction_panel import InteractionPanel, run_panel


class _PanelApp(App):
    """Minimal stand-in for the main screen: a scrollable #log above a
    #status-bar, matching where run_panel mounts panels in the real app."""

    def __init__(self) -> None:
        super().__init__()
        self.result = "unset"
        self.panel = InteractionPanel()
        self.worker = None

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 200), id="log")
        yield Static("status", id="status-bar")

    def on_mount(self) -> None:
        self.worker = self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(self, self.panel)


@pytest.mark.anyio
async def test_resolve_returns_value_and_removes_panel():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.panel.is_attached  # mounted while pending
        app.panel.resolve({"answer": 42})
        await pilot.pause()
        assert app.result == {"answer": 42}
        assert not app.panel.is_attached  # removed after resolution


@pytest.mark.anyio
async def test_panel_mounts_above_status_bar():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        children = list(app.screen.children)
        assert children.index(app.panel) < children.index(
            app.query_one("#status-bar")
        )


@pytest.mark.anyio
async def test_worker_cancel_removes_panel():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.panel.is_attached
        app.worker.cancel()
        await pilot.pause()
        assert not app.panel.is_attached
        assert app.result == "unset"  # never resolved


@pytest.mark.anyio
async def test_double_resolve_is_harmless():
    app = _PanelApp()
    async with app.run_test() as pilot:
        await pilot.pause()
        app.panel.resolve("first")
        app.panel.resolve("second")  # must not raise InvalidStateError
        await pilot.pause()
        assert app.result == "first"


@pytest.mark.anyio
async def test_scroll_keys_forward_to_transcript():
    app = _PanelApp()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Focus inside the panel: scroll keys must still reach the transcript.
        app.panel.can_focus = True
        app.panel.focus()
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        await pilot.press("pageup")
        await pilot.pause()
        assert log.scroll_y == 0
        await pilot.press("ctrl+down")
        await pilot.pause()
        assert log.scroll_y > 0
