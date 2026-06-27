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
            TextPart(content="hello"), None, record, {}, None, None
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
            TextPart(content=""), None, record, {}, sentinel, sentinel  # type: ignore[arg-type]
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
        await sv._replay_parts(part, None, None, tool_widgets, None, None)
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
        await sv._replay_parts(part, None, None, {}, None, None)


@pytest.mark.anyio
async def test_replay_parts_spawn_agent_mounts_widget_no_pane(tmp_path: Path):
    """A foreground spawn_agent in _replay_parts mounts a SubAgentWidget but
    never creates a SubAgentDetailHost pane — pane creation is main-log-only
    and lives in replay_history after the _replay_parts call returns."""
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

        await sv._replay_parts(part, None, record, tool_widgets, None, None)

        assert len(mounted) == 1
        assert isinstance(mounted[0], SubAgentWidget)
        # _replay_parts never creates panes — that's replay_history's job
        panes_after = len(list(host.query("SubAgentPane")))
        assert panes_after == panes_before


@pytest.mark.anyio
async def test_parity_replay_history_and_replay_messages_into(tmp_path: Path):
    """Both replay_history and replay_messages_into produce identical widget types
    for a ModelResponse containing TextPart and ToolCallPart.

    This is the key regression guard: any path-specific deviation in _replay_parts
    dispatch would surface here as a type mismatch.
    """
    from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
    from textual.containers import VerticalScroll

    from marim_harness.interfaces.tui.widgets import AssistantMessage, ToolCallWidget
    from marim_harness.interfaces.tui.widgets.subagent_detail import SubAgentDetailHost

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        messages = [
            ModelResponse(parts=[
                TextPart(content="hello"),
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": "foo.py"},
                    tool_call_id="call-parity-1",
                ),
            ])
        ]

        # -- replay_history path: mount to a fresh VerticalScroll --
        fresh_log = VerticalScroll()
        await app.mount(fresh_log)
        app.harness.session.history = messages  # type: ignore[assignment]
        await sv.replay_history(fresh_log)
        rh_types = [type(w) for w in fresh_log.children]

        # -- replay_messages_into path: mount to a SubAgentPane --
        host = app.query_one(SubAgentDetailHost)
        pane = host.add_pane("parity-pane", "claude", "", "", "")
        await pilot.pause()  # let the pane's initial children mount
        before_pane = len(list(pane.children))
        await sv.replay_messages_into(pane, messages)
        # Only the widgets added by replay_messages_into (after the fixed headers)
        rmi_types = [type(w) for w in list(pane.children)[before_pane:]]

        assert rh_types == rmi_types
        assert rh_types == [AssistantMessage, ToolCallWidget]


@pytest.mark.anyio
async def test_replay_parts_text_resets_group_solo_with_prior_state(tmp_path: Path):
    """TextPart resets group and solo even when they were non-None on entry.

    The original replay_messages_into omitted this reset, so a tool call after
    text output in a sub-agent pane would be incorrectly grouped with tools
    before the text. The shared _replay_parts helper fixes this for both paths.
    """
    from pydantic_ai.messages import TextPart

    from marim_harness.interfaces.tui.widgets import ToolCallWidget

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sv = app.session

        mounted: list = []

        async def record(w):
            mounted.append(w)

        fake_solo = MagicMock(spec=ToolCallWidget)
        group, solo = await sv._replay_parts(
            TextPart(content="text after tools"), None, record, {}, None, fake_solo
        )
        assert group is None
        assert solo is None
