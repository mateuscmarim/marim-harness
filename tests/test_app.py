from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tui.app import HarnessApp


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_status_bar_shows_mode(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        bar = app.query_one("#status-bar")
        text = str(bar.render())
        assert "ask" in text or "auto" in text


@pytest.mark.anyio
async def test_mode_keybinding_cycles(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        start = app.harness.deps.mode
        await pilot.press("ctrl+t")
        await pilot.pause()
        assert app.harness.deps.mode is not start
