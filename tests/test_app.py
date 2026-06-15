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


@pytest.mark.anyio
async def test_on_events_mounts_and_finishes_tool_widget(tmp_path: Path):
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    from marim_harness.tui.widgets import ToolCallWidget

    call = FunctionToolCallEvent(
        part=ToolCallPart(
            tool_name="read_file",
            args={"path": "a.txt"},
            tool_call_id="call-1",
        )
    )
    result = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="read_file",
            content="1\tfoo",
            tool_call_id="call-1",
        )
    )

    async def gen():
        yield call
        yield result

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await app._on_events(None, gen())
        await pilot.pause()

        widget = app._tool_widgets.get("call-1")
        assert isinstance(widget, ToolCallWidget)
        log = app.query_one("#log")
        assert widget in log.walk_children()
        assert widget.status == "done"
        assert "1\tfoo" in widget.result_text
