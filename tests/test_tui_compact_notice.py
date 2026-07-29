"""Tests for the CompactNotice reactive widget."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.compact_notice import CompactNotice


class _CompactApp(App[None]):
    def compose(self) -> ComposeResult:
        yield CompactNotice()


@pytest.mark.anyio
async def test_hidden_by_default():
    """CompactNotice is hidden on mount."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        assert notice.display is False


@pytest.mark.anyio
async def test_compacting_shows():
    """Setting compacting=True shows the notice."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        notice.compacting = True
        await pilot.pause()
        assert notice.display is True
        text = notice.render()
        assert "compacting" in str(text).lower()


@pytest.mark.anyio
async def test_compacting_false_hides():
    """Setting compacting=False hides the notice."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        notice.compacting = True
        await pilot.pause()
        notice.compacting = False
        await pilot.pause()
        assert notice.display is False


@pytest.mark.anyio
async def test_done_shows_checkmark():
    """Setting done=True shows a checkmark briefly."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        notice.done = True
        await pilot.pause()
        assert notice.display is True
        text = notice.render()
        assert "✓" in str(text) or "done" in str(text).lower()


@pytest.mark.anyio
async def test_error_shows_message():
    """Setting error_msg shows an error notice."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        notice.error_msg = "compaction failed"
        await pilot.pause()
        assert notice.display is True
        text = notice.render()
        assert "compaction failed" in str(text)


@pytest.mark.anyio
async def test_compacting_false_clears_error():
    """Setting compacting=False after error hides the error."""
    async with _CompactApp().run_test() as pilot:
        notice = pilot.app.query_one(CompactNotice)
        notice.error_msg = "compaction failed"
        await pilot.pause()
        notice.compacting = False
        await pilot.pause()
        assert notice.display is False
