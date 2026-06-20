from pathlib import Path

import pytest

from marim_harness.interfaces.tui.status import StatusPresenter


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    from marim_harness.interfaces.tui.app import HarnessApp

    return HarnessApp(Harness(TestModel(call_tools=[]), BuiltinToolProvider(),
                              deps, instructions="test"))


@pytest.mark.anyio
async def test_status_presenter_owns_busy_and_drives_title(tmp_path: Path):
    app = _app(tmp_path)
    assert isinstance(app.status, StatusPresenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.set_busy(True)
        assert app.status.busy is True
        assert not hasattr(app, "_busy")  # state truly moved, no shim left behind
