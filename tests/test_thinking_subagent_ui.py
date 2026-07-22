"""The sub-agent thinking level is reported to the UI and shown on the card
(only when a real level resolves — off/none stays silent). The report itself is
an extracted helper so it's testable without the full spawn machinery."""

from unittest.mock import MagicMock

import pytest
from pydantic_ai.models.test import TestModel
from pydantic_ai.settings import ModelSettings

from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.permissions import Mode
from marim_harness.subagents.runner import SubagentRunner
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.workspace.agents import AgentDef


def test_uihooks_has_on_subagent_thinking():
    hooks = UIHooks()
    assert hooks.on_subagent_thinking is None  # optional, None when no UI


def test_card_set_thinking_level_updates_label():
    from marim_harness.interfaces.tui.subagents.card import SubAgentWidget

    widget = SubAgentWidget("coder", "do it")
    widget.set_thinking_level("high")
    assert widget.thinking_label == "high"


def test_bind_ui_wires_on_subagent_thinking(tmp_path):
    """bind_ui lands the thinking callback on deps.ui, so the runner's report
    reaches the TUI at runtime — not just in unit tests. Without this wiring the
    card would never annotate the resolved level (the callback stays None)."""
    from tests.conftest import _make_deps, _make_harness, _text_model

    deps = _make_deps(tmp_path)
    h = _make_harness(_text_model(), deps)

    async def sink(stream_id: str, level: str) -> None: ...

    h.bind_ui(on_subagent_thinking=sink)
    assert h.deps.ui.on_subagent_thinking is sink


def _runner(tmp_path, on_thinking, **kwargs) -> SubagentRunner:
    deps = Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))
    deps.ui.on_subagent_thinking = on_thinking
    return SubagentRunner(
        BuiltinToolProvider(), MagicMock(), deps, MagicMock(), MagicMock(),
        get_model=lambda: TestModel(call_tools=[]),
        model_settings=ModelSettings(parallel_tool_calls=True),
        **kwargs,
    )


def _spec(thinking: str | None) -> AgentDef:
    return AgentDef(
        name="coder", description="", prompt="Go.",
        tools=frozenset({"read_file"}), source="built-in", thinking=thinking,
    )


@pytest.mark.anyio
async def test_report_fires_for_resolved_inherited_level(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "high")
    await runner._report_spawn_thinking("stream-1", None, _spec(None))
    assert reported == [("stream-1", "high")]


@pytest.mark.anyio
async def test_report_override_beats_spec(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "low")
    await runner._report_spawn_thinking("stream-1", "medium", _spec("high"))
    assert reported == [("stream-1", "medium")]


@pytest.mark.anyio
async def test_report_silent_for_off_and_none(tmp_path):
    reported: list = []

    async def on_thinking(stream_id, level):
        reported.append((stream_id, level))

    runner = _runner(tmp_path, on_thinking, thinking_default=lambda: "high")
    await runner._report_spawn_thinking("stream-1", "off", _spec(None))  # explicit off
    runner_none = _runner(tmp_path, on_thinking)  # no inherited, no spec
    await runner_none._report_spawn_thinking("stream-2", None, _spec(None))
    assert reported == []
