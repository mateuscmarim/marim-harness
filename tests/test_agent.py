from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.agent import Harness
from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from marim_harness.tools.provider import BuiltinToolProvider


def _edit_then_done_model() -> FunctionModel:
    """First model turn: call edit_file. After the tool result: reply 'done'."""
    state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(
                parts=[
                    ToolCallPart(
                        tool_name="edit_file",
                        args={"path": "a.txt", "old_string": "foo", "new_string": "bar"},
                    )
                ]
            )
        return ModelResponse(parts=[TextPart(content="done")])

    return FunctionModel(fn)


def _make_harness(model, deps) -> Harness:
    return Harness(model=model, provider=BuiltinToolProvider(), deps=deps,
                   instructions="You are a coding agent.")


@pytest.mark.anyio
async def test_auto_mode_applies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "bar"


@pytest.mark.anyio
async def test_plan_mode_denies_edit(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    deps = Deps(workspace_root=tmp_path, mode=Mode.plan)
    harness = _make_harness(_edit_then_done_model(), deps)
    output = await harness.run_turn("change foo to bar")
    assert output == "done"
    assert (tmp_path / "a.txt").read_text() == "foo"  # unchanged


@pytest.mark.anyio
async def test_ask_mode_calls_back(tmp_path: Path):
    (tmp_path / "a.txt").write_text("foo")
    asked = []

    async def approve(call):
        asked.append(call.tool_name)
        return True

    deps = Deps(workspace_root=tmp_path, mode=Mode.ask, request_approval=approve)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    assert asked == ["edit_file"]
    assert (tmp_path / "a.txt").read_text() == "bar"
