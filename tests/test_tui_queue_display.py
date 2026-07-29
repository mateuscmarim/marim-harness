"""Tests for the QueueDisplay reactive widget."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.queue import QueuedMessage
from marim_harness.interfaces.tui.widgets.queue_display import QueueDisplay


class _QueueApp(App[None]):
    def compose(self) -> ComposeResult:
        yield QueueDisplay()


@pytest.mark.anyio
async def test_hidden_when_empty():
    """QueueDisplay is hidden when items list is empty."""
    async with _QueueApp().run_test() as pilot:
        qd = pilot.app.query_one(QueueDisplay)
        assert qd.display is False


@pytest.mark.anyio
async def test_shows_items():
    """Setting items shows the queue."""
    async with _QueueApp().run_test() as pilot:
        qd = pilot.app.query_one(QueueDisplay)
        qd.items = [QueuedMessage("hello", None, "1")]
        await pilot.pause()
        assert qd.display is True
        text = qd.render()
        assert "hello" in str(text)


@pytest.mark.anyio
async def test_paused_badge():
    """Setting paused=True shows a paused indicator."""
    async with _QueueApp().run_test() as pilot:
        qd = pilot.app.query_one(QueueDisplay)
        qd.items = [QueuedMessage("hello", None, "1")]
        qd.paused = True
        await pilot.pause()
        text = qd.render()
        assert "paused" in str(text).lower()


@pytest.mark.anyio
async def test_hides_when_items_cleared():
    """Clearing items hides the queue."""
    async with _QueueApp().run_test() as pilot:
        qd = pilot.app.query_one(QueueDisplay)
        qd.items = [QueuedMessage("hello", None, "1")]
        await pilot.pause()
        qd.items = []
        await pilot.pause()
        assert qd.display is False


@pytest.mark.anyio
async def test_multiple_items():
    """Multiple items are all rendered."""
    async with _QueueApp().run_test() as pilot:
        qd = pilot.app.query_one(QueueDisplay)
        qd.items = [
            QueuedMessage("first", None, "1"),
            QueuedMessage("second", None, "2"),
        ]
        await pilot.pause()
        text = qd.render()
        assert "first" in str(text)
        assert "second" in str(text)
