from pathlib import Path

import pytest

from marim_harness.interfaces.tui.widgets.status_bar import StatusBar
from tests.conftest import _make_deps


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    from marim_harness.interfaces.tui.app import HarnessApp

    return HarnessApp(Harness(TestModel(call_tools=[]), BuiltinToolProvider(),
                              deps, instructions="test"))


@pytest.mark.anyio
async def test_status_presenter_owns_busy_and_drives_title(tmp_path: Path):
    app = _app(tmp_path)
    assert isinstance(app.status, StatusBar)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.set_busy(True)
        assert app.status.busy is True
        assert not hasattr(app, "_busy")  # state truly moved, no shim left behind


def test_context_tokens_memoized_until_history_changes(monkeypatch):
    """The status bar repaints every second while idle and ~12.5x/s while a turn
    streams. estimate_tokens() serializes the *whole* history (O(total bytes)), so
    it must be cached and recomputed only when the history actually changes —
    re-stringifying the transcript on every repaint is pure waste that grows with
    session length."""
    from textual._context import active_app

    import marim_harness.interfaces.tui.widgets.status_bar as status_bar_mod

    calls = {"n": 0}

    def fake_estimate(history):
        calls["n"] += 1
        return len(history) * 10

    monkeypatch.setattr(status_bar_mod, "estimate_tokens", fake_estimate)

    history = [object(), object()]

    class _App:
        class harness:
            class session:
                pass

    _App.harness.session.history = history

    # A bare StatusBar() (never mounted) needs an app to resolve `self.app`
    # against — set it via Textual's active_app context var rather than
    # assigning the (read-only) `app` property directly.
    token = active_app.set(_App())  # type: ignore[arg-type]
    try:
        presenter = StatusBar()
        presenter._ctx_tokens_key = -1
        presenter._ctx_tokens = 0

        # Repeated reads (every repaint) recompute the estimate exactly once.
        assert presenter._context_tokens() == 20
        assert presenter._context_tokens() == 20
        assert calls["n"] == 1

        # A committed message grows the history → recompute exactly once more.
        history.append(object())
        assert presenter._context_tokens() == 30
        assert calls["n"] == 2
    finally:
        active_app.reset(token)


def test_refresh_title_swallows_driver_errors_during_teardown():
    """refresh_title runs from set_busy in _run_turn's finally block. A driver
    mid-teardown can raise BrokenPipeError on write/flush; it must not escape,
    or _after_turn() is skipped and the queue/wake chain stalls."""
    from textual._context import active_app

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

    fake_app = _App()
    token = active_app.set(fake_app)  # type: ignore[arg-type]
    try:
        presenter = StatusBar()
        presenter.busy = False
        presenter.spin = 0

        # Must not raise — the BrokenPipeError is swallowed best-effort.
        presenter.refresh_title()
        assert fake_app.title == "● s"  # in-app Header still updated
    finally:
        active_app.reset(token)
