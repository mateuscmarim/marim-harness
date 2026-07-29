"""Tests for the StatusBar reactive widget."""
from __future__ import annotations

import pytest
from pydantic_ai.usage import RunUsage
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.status_bar import StatusBar


class _StatusBarApp(App[None]):
    """Minimal app harness for testing StatusBar in isolation.

    StatusBar.render() reads a few fields off ``self.app.harness`` (context
    tokens, cost, model label) — stub the minimal shape here rather than
    building a full HarnessApp.
    """

    def compose(self) -> ComposeResult:
        yield StatusBar()

    def on_mount(self) -> None:
        class _Session:
            history: list = []
            usage = RunUsage()
            compact_threshold = 0

        class _Harness:
            session = _Session()
            model_id = None
            model_label = "test-model"

        self.harness = _Harness()


@pytest.mark.anyio
async def test_status_bar_mounts():
    """StatusBar renders on mount."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        assert bar is not None
        assert isinstance(bar, Static)


@pytest.mark.anyio
async def test_busy_reactive_shows_spinner():
    """Setting busy=True renders the working indicator."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.busy = True
        await pilot.pause()
        text = bar.render()
        assert "working" in str(text).lower() or "…" in str(text)


@pytest.mark.anyio
async def test_busy_false_hides_spinner():
    """Setting busy=False hides the working indicator."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.busy = True
        await pilot.pause()
        bar.busy = False
        await pilot.pause()
        text = bar.render()
        assert "working" not in str(text).lower()


@pytest.mark.anyio
async def test_mode_reactive():
    """Mode value appears in the status text."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.mode = "auto"
        await pilot.pause()
        text = bar.render()
        assert "auto" in str(text)


@pytest.mark.anyio
async def test_model_name_reactive():
    """Model name appears in the status text."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.model_name = "claude-sonnet"
        await pilot.pause()
        text = bar.render()
        assert "claude-sonnet" in str(text)


@pytest.mark.anyio
async def test_live_tokens_delta():
    """Live token count shows as +N delta."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.live_run_tokens = 1500
        await pilot.pause()
        text = bar.render()
        assert "+1" in str(text)  # +1.5k or similar


@pytest.mark.anyio
async def test_ttft_display():
    """Time-to-first-token appears when set."""
    async with _StatusBarApp().run_test() as pilot:
        bar = pilot.app.query_one(StatusBar)
        bar.last_ttft = 0.8
        await pilot.pause()
        text = bar.render()
        assert "0.8" in str(text)
