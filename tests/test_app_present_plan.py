"""The app wires on_present_plan: a present_plan handoff mounts a PlanCard whose
choice flips the session mode end to end."""

import json
import time

import anyio
import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import DeltaToolCall, FunctionModel
from textual.widgets import OptionList

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.interactions.plan_card import PlanCard
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


async def _settle(pilot, predicate, *, what: str, timeout: float = 10.0) -> None:
    """Pump the app until ``predicate`` holds, or fail naming what never happened.

    A fixed count of ``pilot.pause()`` calls is not a wait. A pause yields to the
    message pump and returns, so a spin of fifty can be over in microseconds and
    never hand a loaded runner the slice it was short of — which is exactly how
    the fixed spin this replaces passed on a fast machine and failed twice on CI.
    Bounded by the clock, with a real sleep between attempts, it waits for the
    condition rather than for a number of trips round the loop.
    """
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        await pilot.pause()
        if predicate():
            return
        await anyio.sleep(0.01)
    raise AssertionError(f"timed out after {timeout}s waiting for {what}")


async def test_present_plan_mounts_card_and_flips_mode(tmp_path):
    (tmp_path / "a.txt").write_text("x")
    deps = _make_deps(tmp_path, mode=Mode.plan)
    harness = _make_harness(_plan_then_done_model(), deps)
    app = HarnessApp(harness)
    async with app.run_test() as pilot:
        app.run_worker(app._run_turn("plan the refactor"))
        await _settle(pilot, lambda: bool(app.query(PlanCard)),
                      what="the PlanCard to mount")
        # Being in the DOM is not the same as being ready for a keypress: the
        # card highlights its first choice and takes focus from its own
        # on_mount, one message later. Press enter before that and the key goes
        # to whatever had focus before — the prompt input — where it does
        # nothing, and the choice is lost with no trace. That is what the CI
        # failure this replaces actually was: its stack dump had the turn still
        # parked in run_panel awaiting a decision 120 seconds later, so the
        # keypress had gone missing rather than merely arrived late.
        await _settle(pilot, lambda: isinstance(app.focused, OptionList),
                      what="the plan choices to take focus")
        await pilot.press("enter")  # highlighted = "Execute hands-off (auto)"
        await _settle(pilot, lambda: deps.workspace.mode is Mode.auto,
                      what="the choice to flip the session mode")
    assert deps.workspace.mode is Mode.auto
    assert deps.plan is not None and deps.plan.summary == "Refactor the parser."
