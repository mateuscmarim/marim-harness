"""Tests for the non-blocking notification path and that the TUI's ``_notify``
dispatches OFF the event loop (it must never call the blocking ``subprocess.run``
synchronously from a callback, or the Windows backend alone freezes the UI ~5.5s).
"""

from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import pytest

from marim_harness.notifications import (
    EVENT_TURN_COMPLETE,
    NotificationConfig,
    Notifier,
)
from tests.conftest import _make_deps

# ---------------------------------------------------------------------------
# Notifier.send_async — non-blocking dispatch
# ---------------------------------------------------------------------------


@pytest.mark.anyio
async def test_send_async_spawns_subprocess_not_blocking_run():
    """The async path must use asyncio's subprocess spawn, never the blocking
    ``subprocess.run`` (which would stall the event loop)."""
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"

    class _FakeProc:
        async def communicate(self, stdin=None):
            return (b"", b"")

    async def _fake_exec(*argv, **kwargs):
        _fake_exec.argv = argv
        return _FakeProc()

    with patch(
        "marim_harness.notifications.shutil.which",
        return_value="/usr/bin/notify-send",
    ), patch(
        "marim_harness.notifications.asyncio.create_subprocess_exec",
        side_effect=_fake_exec,
    ) as mock_exec, patch(
        "marim_harness.notifications.subprocess.run"
    ) as mock_run:
        await n.send_async("Title", "Body", EVENT_TURN_COMPLETE)

    mock_run.assert_not_called()  # never the blocking path
    mock_exec.assert_called_once()
    assert _fake_exec.argv[0] == "notify-send"


@pytest.mark.anyio
async def test_send_async_respects_disabled_and_coalesce():
    n = Notifier(NotificationConfig(enabled=False))
    with patch(
        "marim_harness.notifications.asyncio.create_subprocess_exec"
    ) as mock_exec:
        await n.send_async("t", "b", EVENT_TURN_COMPLETE)
    mock_exec.assert_not_called()


@pytest.mark.anyio
async def test_send_async_swallows_errors_and_rolls_back_timestamp():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"
    with patch(
        "marim_harness.notifications.shutil.which",
        return_value="/usr/bin/notify-send",
    ), patch(
        "marim_harness.notifications.asyncio.create_subprocess_exec",
        side_effect=OSError("boom"),
    ):
        await n.send_async("t", "b", EVENT_TURN_COMPLETE)  # must not raise
    # On failure the coalesce timestamp is rolled back so a retry isn't suppressed.
    assert EVENT_TURN_COMPLETE not in n._last_fired


@pytest.mark.anyio
async def test_send_async_unknown_platform_is_noop():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "weirdos"
    with patch(
        "marim_harness.notifications.asyncio.create_subprocess_exec"
    ) as mock_exec:
        await n.send_async("t", "b", EVENT_TURN_COMPLETE)
    mock_exec.assert_not_called()
    # Nothing fired, so the coalesce timestamp must be rolled back.
    assert EVENT_TURN_COMPLETE not in n._last_fired


@pytest.mark.anyio
async def test_send_async_missing_binary_rolls_back_timestamp():
    n = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    n._platform = "linux"
    with patch(
        "marim_harness.notifications.shutil.which", return_value=None
    ), patch(
        "marim_harness.notifications.asyncio.create_subprocess_exec"
    ) as mock_exec:
        await n.send_async("t", "b", EVENT_TURN_COMPLETE)
    mock_exec.assert_not_called()
    assert EVENT_TURN_COMPLETE not in n._last_fired


# ---------------------------------------------------------------------------
# HarnessApp._notify — must dispatch off the event loop
# ---------------------------------------------------------------------------


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_notify_does_not_call_blocking_send_synchronously(tmp_path: Path):
    """``_notify`` is invoked from event-loop callbacks; it must NOT call the
    blocking ``Notifier.send`` directly — it must schedule the async path."""
    app = _app(tmp_path)
    notifier = Notifier(NotificationConfig(enabled=True, events={"turn_complete"}))
    app.harness.deps.ui.notifier = notifier

    async with app.run_test():
        with patch.object(notifier, "send") as blocking_send, patch.object(
            notifier, "send_async", wraps=notifier.send_async
        ) as async_send, patch(
            "marim_harness.notifications.asyncio.create_subprocess_exec"
        ):
            app.activity.desktop_notify("Turn complete", "done", EVENT_TURN_COMPLETE)
            blocking_send.assert_not_called()
            async_send.assert_called_once()


@pytest.mark.anyio
async def test_notify_noop_when_notifier_absent(tmp_path: Path):
    app = _app(tmp_path)
    app.harness.deps.ui.notifier = None
    async with app.run_test():
        # Must not raise when no notifier is wired.
        app.activity.desktop_notify("Turn complete", "done", EVENT_TURN_COMPLETE)
