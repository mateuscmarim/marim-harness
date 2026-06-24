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


def test_refresh_title_swallows_driver_errors_during_teardown():
    """refresh_title runs from set_busy in _run_turn's finally block. A driver
    mid-teardown can raise BrokenPipeError on write/flush; it must not escape,
    or _after_turn() is skipped and the queue/wake chain stalls."""

    class _BrokenDriver:
        def write(self, _text):
            raise BrokenPipeError("driver tearing down")

        def flush(self):
            raise BrokenPipeError("driver tearing down")

    class _Session:
        session_name = "s"

    class _Harness:
        session = _Session()

    class _App:
        title = ""
        harness = _Harness()
        _driver = _BrokenDriver()

    presenter = StatusPresenter.__new__(StatusPresenter)
    presenter.app = _App()  # type: ignore[assignment]
    presenter.busy = False
    presenter.spin = 0

    # Must not raise — the BrokenPipeError is swallowed best-effort.
    presenter.refresh_title()
    assert presenter.app.title == "● s"  # in-app Header still updated
