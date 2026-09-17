"""Advisor selection, rendering and migration documentation contracts."""

from pathlib import Path
from types import SimpleNamespace

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
)

from tests.test_app import _app


@pytest.mark.anyio
async def test_advisor_selection_notices(tmp_path):
    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.interfaces.tui.commands import dispatch
    from marim_harness.interfaces.tui.pickers import ModelPickers
    from tests.test_advisor_command import _App

    app = _App()
    for argument in ("reviewer", "off"):
        await dispatch(app, f"/advisor {argument}")
        assert "next turn" in app.posted[-1]
    await dispatch(app, "/advisor")
    assert app.picker_opened
    app.harness.model_id = "codex-cli:executor"
    await dispatch(app, "/advisor reviewer")
    assert "unavailable" in app.posted[-1]

    notices = []
    app.append_log = lambda widget: notices.append(str(widget.render()))
    app.link = SimpleNamespace(
        info=SimpleNamespace(
            model_id="claude-cli:executor", advisor_model_id="reviewer", thinking_level_id=None
        )
    )
    picker = ModelPickers(app)
    for choice in ("reviewer", "off"):
        picker.on_advisor_chosen(choice)
        assert "next turn" in notices[-1] and "unavailable" in notices[-1]
    HarnessApp._announce_session_defaults(app)
    assert "next turn" in notices[-1] and "unavailable" in notices[-1]


@pytest.mark.anyio
@pytest.mark.parametrize("replay", [False, True])
async def test_advice_live_and_replay_standalone(tmp_path, replay):
    from marim_harness.interfaces.tui.widgets import ToolCallWidget, ToolGroupWidget

    calls = [
        ToolCallPart("read_file", {"path": "a.py"}, "r1"),
        ToolCallPart("read_file", {"path": "b.py"}, "r2"),
        ToolCallPart("advisor", {"prompt": "Review"}, "a1"),
        ToolCallPart("read_file", {"path": "c.py"}, "r3"),
    ]
    results = [
        ToolReturnPart(
            c.tool_name,
            "Check the rollback." if c.tool_name == "advisor" else "content",
            c.tool_call_id,
        )
        for c in calls
    ]
    app = _app(tmp_path)
    if replay:
        app.harness.session.history = [ModelResponse(parts=calls), ModelRequest(parts=results)]

    async def events():
        for call, result in zip(calls, results, strict=True):
            yield FunctionToolCallEvent(call)
            yield FunctionToolResultEvent(result)

    async with app.run_test() as pilot:
        await pilot.pause()
        if not replay:
            await app.stream.on_events(None, events())
            await pilot.pause()
        advice = [w for w in app.query(ToolCallWidget) if w.tool_name == "advisor"]
        assert len(advice) == 1
        assert advice[0].result_text == "Check the rollback."
        assert advice[0].status == "done"
        groups = list(app.query(ToolGroupWidget))
        assert groups
        assert all(advice[0] not in g.walk_children() for g in groups)


def test_advisor_documentation_contract():
    root = Path(__file__).parents[1]
    docs = "\n".join(
        (root / path).read_text()
        for path in (
            "docs/embedding.md",
            "docs/sdk/capabilities.md",
            "docs/reference/configuration.md",
            "docs/guides/tui.md",
        )
    )
    for phrase in (
        "advisor(prompt",
        "per model request",
        "1024",
        "next turn",
        "errors propagate",
        "local execution",
        "CLI main",
    ):
        assert phrase in docs
    assert "from pydantic_ai_harness import Advisor" in docs
    assert "Both paths share the same consult core" not in docs
    assert "next consultation" not in docs


def test_settings_advisor_bounds_and_labels():
    from marim_harness.interfaces.tui.settings_env import (
        ENV_INT_INPUTS,
        EnvAutoSave,
        parse_int_field,
    )

    assert ENV_INT_INPUTS["advisor-max-uses"][1] == "Advisor calls per model request"
    assert parse_int_field("advisor-max-tokens", "1023") is None
    assert parse_int_field("advisor-max-tokens", "1024") == 1024
    notices = []
    saves = []
    form = EnvAutoSave(notices.append)
    form.commit = lambda key, value: saves.append((key, value))
    form.commit_int("advisor-max-tokens", "1023")
    assert saves == [] and "1024" in notices[-1]
    form.commit_int("advisor-max-tokens", "1024")
    assert saves == [("MARIM_ADVISOR_MAX_TOKENS", "1024")]
