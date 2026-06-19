# tests/test_image_paste.py
import pytest

from marim_harness.interfaces.tui.app import HarnessApp


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_ctrl_v_invokes_paste_image_hook(tmp_path):
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        calls = []
        box._on_paste_image = lambda: (calls.append(1), False)[1]
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert calls == [1]
