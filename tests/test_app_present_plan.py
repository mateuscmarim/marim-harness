"""The app wires on_present_plan: a present_plan handoff mounts a PlanCard whose
choice flips the session mode end to end."""

import json

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.plan_card import PlanCard
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps, _make_harness

pytestmark = pytest.mark.anyio


def _plan_then_done_model() -> FunctionModel:
    """present_plan then a final text reply. The app drives turns through
    ``agent.run`` with a live event-stream handler wired, which requires the
    model to support streamed requests (unlike the plain ``TurnController``
    tests) — so this needs a ``stream_function`` alongside ``fn``, mirroring
    ``test_app.test_gated_tool_renders_one_widget_not_two``."""
    state = {"n": 0}
    stream_state = {"n": 0}

    def fn(messages, info):
        state["n"] += 1
        if state["n"] == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="present_plan",
                args={"summary": "Refactor the parser.",
                      "steps": ["Extract tokenizer", "Add tests"]},
                tool_call_id="call_plan")])
        return ModelResponse(parts=[TextPart(content="executing now")])

    async def stream_fn(messages, info):
        stream_state["n"] += 1
        if stream_state["n"] == 1:
            yield {0: DeltaToolCall(
                name="present_plan",
                json_args=json.dumps({
                    "summary": "Refactor the parser.",
                    "steps": ["Extract tokenizer", "Add tests"],
                }),
                tool_call_id="call_plan",
            )}
        else:
            yield "executing now"

    return FunctionModel(fn, stream_function=stream_fn)


async def test_present_plan_mounts_card_and_flips_mode(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    deps = _make_deps(tmp_path, mode=Mode.plan)
    harness = _make_harness(_plan_then_done_model(), deps)
    app = HarnessApp(harness)
    async with app.run_test() as pilot:
        app.run_worker(app._run_turn("plan the refactor"))
        # Wait for the PlanCard to appear.
        for _ in range(50):
            await pilot.pause()
            if app.query(PlanCard):
                break
        assert app.query(PlanCard), "PlanCard never mounted"
        await pilot.press("enter")  # highlighted = "Execute hands-off (auto)"
        for _ in range(50):
            await pilot.pause()
            if deps.workspace.mode is Mode.auto:
                break
    assert deps.workspace.mode is Mode.auto
    assert deps.plan is not None and deps.plan.summary == "Refactor the parser."
