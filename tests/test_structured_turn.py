"""Structured output for embedder turns (HarnessBuilder.with_output_type).

These drive a builder-built Harness through real turns with pydantic-ai's
`TestModel`, asserting the `TurnOutcome` shape `run_turn` now returns: plain
harnesses get text in `result`, structured ones get the validated object in
`structured_output` — including across an approval round, where the per-run
output override has to ride the continuation call too.

`call_tools=[]` is deliberate: `TestModel` otherwise calls *every* registered
tool on the first round, which would drown these turns in unrelated work.
"""

import pytest
from pydantic import BaseModel
from pydantic_ai import RunContext
from pydantic_ai.models.test import TestModel

from marim_harness import Deps, HarnessBuilder

pytestmark = pytest.mark.anyio  # tests/test_builder_turns.py uses the same marker


class Point(BaseModel):
    a: int


async def _run(harness, prompt):
    await harness.connect()
    try:
        return await harness.run_turn(prompt)
    finally:
        await harness.aclose()


async def test_plain_turn_outcome_shape(tmp_path):
    h = HarnessBuilder(
        workspace=tmp_path,
        model=TestModel(call_tools=[], custom_output_text="hello there"),
    ).build()
    outcome = await _run(h, "say hi")
    assert outcome.subtype == "success"
    assert outcome.result == "hello there"
    assert outcome.structured_output is None
    assert outcome.errors is None


async def test_basemodel_structured_turn(tmp_path):
    h = (
        HarnessBuilder(
            workspace=tmp_path,
            model=TestModel(call_tools=[], custom_output_args={"a": 3}),
        )
        .with_output_type(Point)
        .build()
    )
    outcome = await _run(h, "give me the point")
    assert outcome.subtype == "success"
    assert outcome.structured_output == Point(a=3)
    assert outcome.result is None


async def test_structured_turn_survives_approval_round(tmp_path):
    """A gated tool auto-approved mid-turn does not disturb the structured
    result: the per-run output override must ride the continuation round too."""
    calls: list[str] = []

    def touch_file(ctx: RunContext[Deps], path: str) -> str:
        """Touch `path` in the workspace."""
        calls.append(path)
        return "touched"

    h = (
        HarnessBuilder(
            workspace=tmp_path,
            model=TestModel(call_tools=["touch_file"], custom_output_args={"a": 7}),
        )
        .with_tool(touch_file, requires_approval=True)
        .with_output_type(Point)
        .build()
    )
    outcome = await _run(h, "touch the file, then report")
    assert calls  # the gated tool really ran (auto mode approves)
    assert outcome.subtype == "success"
    assert outcome.structured_output == Point(a=7)


async def test_basemodel_exhaustion_returns_error_subtype(tmp_path):
    """custom_output_args violates the model on every attempt, so pydantic-ai
    exhausts its retries and the turn ends as an error outcome, not a raise."""
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(call_tools=[], custom_output_args={"a": "not-an-int"}))
         .with_output_type(Point)
         .build())
    outcome = await _run(h, "give me the point")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert outcome.structured_output is None
    assert outcome.errors


async def test_basemodel_exhaustion_persists_first_turn_history(tmp_path):
    """A brand-new session (empty history going in) that exhausts on its very
    first turn must still end up with the exchange on disk — the exhaustion
    branch used to gate the flush on `self.session.history` being non-empty,
    which is exactly false on a first turn, silently dropping the user's
    prompt and the model's failed attempts."""
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(call_tools=[], custom_output_args={"a": "not-an-int"}))
         .with_output_type(Point)
         .build())
    outcome = await _run(h, "give me the point")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert len(h.session.history) >= 2


async def test_exhaustion_on_continuation_round_clears_approval_latch(tmp_path):
    """Exhaustion striking on the round *after* an approval round (rather than
    on a first, clean round) must still drop the dirty-history latch — the
    same reset _handle_run_failure's terminal path performs — so a later
    background force-persist isn't suppressed forever."""

    def touch_file(ctx: RunContext[Deps], path: str) -> str:
        """Touch `path` in the workspace."""
        return "touched"

    h = (
        HarnessBuilder(
            workspace=tmp_path,
            model=TestModel(
                call_tools=["touch_file"], custom_output_args={"a": "not-an-int"}
            ),
        )
        .with_tool(touch_file, requires_approval=True)
        .with_output_type(Point)
        .build()
    )
    outcome = await _run(h, "touch the file, then report")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert h.deps.approval_round_active is False


DICT_SCHEMA = {
    "type": "object",
    "properties": {"a": {"type": "integer"}},
    "required": ["a"],
}


async def test_dict_schema_happy_path(tmp_path):
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(call_tools=[], custom_output_args={"a": 5}))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "success"
    assert outcome.structured_output == {"a": 5}


async def test_dict_schema_corrective_retry_succeeds(tmp_path):
    """First response violates the schema; the corrective round fixes it."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    state = {"calls": 0}

    def fn(messages, info: AgentInfo) -> ModelResponse:
        state["calls"] += 1
        args = {"a": "not-an-int"} if state["calls"] == 1 else {"a": 1}
        # StructuredDict output rides the output tool; its default name is
        # 'final_result' in pydantic-ai (pydantic_ai/_output.py).
        return ModelResponse(parts=[ToolCallPart(tool_name="final_result", args=args)])

    h = (HarnessBuilder(workspace=tmp_path, model=FunctionModel(fn))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "success"
    assert outcome.structured_output == {"a": 1}
    assert state["calls"] == 2


async def test_dict_schema_exhaustion(tmp_path):
    """TestModel emits the same invalid object every round, so the single
    corrective attempt also fails and the turn ends as an error outcome."""
    h = (HarnessBuilder(workspace=tmp_path,
                        model=TestModel(call_tools=[], custom_output_args={"a": "bad"}))
         .with_output_type(DICT_SCHEMA)
         .build())
    outcome = await _run(h, "give me a")
    assert outcome.subtype == "error_max_structured_output_retries"
    assert outcome.structured_output is None
    assert outcome.errors
    assert any("a" in e for e in outcome.errors)
