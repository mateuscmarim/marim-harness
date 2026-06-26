"""Tests for SessionView._replay_parts shared dispatch.

Verifies that the helper used by both replay_history and replay_messages_into
dispatches each shared part type to the correct widget, so behavioral parity
between the two call sites is enforced at the unit level.
"""
from __future__ import annotations

from pathlib import Path
from unittest.mock import MagicMock

import pytest


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_replay_parts_text_mounts_assistant_message(tmp_path: Path):
    """TextPart → AssistantMessage mounted; group/solo reset to None."""
    from pydantic_ai.messages import TextPart

    from marim_harness.interfaces.tui.widgets import AssistantMessage

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        group, solo = await sv._replay_parts(
            TextPart(content="hello"), None, record, {}, None, None, build_pane=False
        )
        assert len(mounted) == 1
        assert isinstance(mounted[0], AssistantMessage)
        assert group is None
        assert solo is None


@pytest.mark.anyio
async def test_replay_parts_empty_text_mounts_nothing(tmp_path: Path):
    """Empty TextPart is skipped — no widget mounted, group/solo unchanged."""
    from pydantic_ai.messages import TextPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        sentinel = object()
        group, solo = await sv._replay_parts(
            TextPart(content=""), None, record, {}, sentinel, sentinel, build_pane=False  # type: ignore[arg-type]
        )
        assert len(mounted) == 0
        # group/solo are unchanged when nothing is mounted
        assert group is sentinel
        assert solo is sentinel


@pytest.mark.anyio
async def test_replay_parts_tool_return_calls_finish(tmp_path: Path):
    """ToolReturnPart looks up the widget in tool_widgets and calls finish()."""
    from pydantic_ai.messages import ToolReturnPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        fake_widget = MagicMock(spec=ToolCallWidget)
        tool_widgets = {"call-abc": fake_widget}

        part = ToolReturnPart(
            tool_name="read_file",
            content="file contents here",
            tool_call_id="call-abc",
        )
        await sv._replay_parts(part, None, None, tool_widgets, None, None, build_pane=False)
        fake_widget.finish.assert_called_once()
        # status arg should be "done" for a successful result
        _, kwargs = fake_widget.finish.call_args
        assert kwargs.get("status") == "done"


@pytest.mark.anyio
async def test_replay_parts_tool_return_unknown_id_is_noop(tmp_path: Path):
    """ToolReturnPart with an unrecognised call_id is silently ignored."""
    from pydantic_ai.messages import ToolReturnPart

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        part = ToolReturnPart(
            tool_name="read_file",
            content="result",
            tool_call_id="unknown-id",
        )
        # Should not raise
        await sv._replay_parts(part, None, None, {}, None, None, build_pane=False)


@pytest.mark.anyio
async def test_replay_parts_build_pane_false_no_pane_created(tmp_path: Path):
    """With build_pane=False, a foreground spawn_agent mounts a SubAgentWidget
    but does NOT create a SubAgentDetailHost pane."""
    from pydantic_ai.messages import ToolCallPart

    from marim_harness.interfaces.tui.widgets import SubAgentWidget
    from marim_harness.interfaces.tui.widgets.subagent_detail import SubAgentDetailHost

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session
        mounted: list = []

        async def record(w):
            mounted.append(w)

        tool_widgets: dict = {}
        part = ToolCallPart(
            tool_name="spawn_agent",
            args={"type": "claude", "description": "do stuff", "background": False},
            tool_call_id="call-spawn-1",
        )

        host = app.query_one(SubAgentDetailHost)
        panes_before = len(list(host.query("SubAgentPane")))

        await sv._replay_parts(part, None, record, tool_widgets, None, None, build_pane=False)

        assert len(mounted) == 1
        assert isinstance(mounted[0], SubAgentWidget)
        # No pane added in sub-agent pane context
        panes_after = len(list(host.query("SubAgentPane")))
        assert panes_after == panes_before
