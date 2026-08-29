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


def _controller(tmp_path, *, structured: bool):
    builder = HarnessBuilder(workspace=tmp_path, model=TestModel(call_tools=[]))
    if structured:
        builder = builder.with_output_type(Point)
    return builder.build().turn_controller


def _validation_error() -> Exception:
    """A real pydantic ValidationError, the cause pydantic-ai attaches to BOTH
    output-retry exhaustion and tool-argument retry exhaustion."""
    try:
        Point.model_validate({"a": "not-an-int"})
    except Exception as exc:  # noqa: BLE001 - re-raised shape is the point
        return exc
    raise AssertionError("Point accepted an invalid payload")


def test_exhaustion_predicate_matches_output_retry_without_validation_cause(tmp_path):
    """`consume_output_retry` (pydantic_ai/_agent_graph.py:371) raises after
    repeated empty / thinking-only responses with a ToolRetryError or None as
    the cause — no ValidationError anywhere in the chain. The documented
    contract says that is still schema exhaustion."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    controller = _controller(tmp_path, structured=True)
    assert controller._is_structured_exhaustion(
        UnexpectedModelBehavior("Exceeded maximum output retries (2)")
    )


def test_exhaustion_predicate_ignores_tool_arg_retry_exhaustion(tmp_path):
    """`ToolManager._check_max_retries` (pydantic_ai/tool_manager.py:237) raises
    `from` the tool's argument ValidationError. That is an infra-shaped failure
    and must keep the generic failure path (error note + provider dump)."""
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    controller = _controller(tmp_path, structured=True)
    exc = UnexpectedModelBehavior(
        "Tool 'bash' exceeded max retries count of 1. Consider raising the retry limit"
    )
    exc.__cause__ = _validation_error()
    assert not controller._is_structured_exhaustion(exc)


def test_exhaustion_predicate_ignores_non_model_behavior_errors(tmp_path):
    controller = _controller(tmp_path, structured=True)
    assert not controller._is_structured_exhaustion(ValueError("boom"))


def test_exhaustion_predicate_off_for_plain_harness(tmp_path):
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    controller = _controller(tmp_path, structured=False)
    assert not controller._is_structured_exhaustion(
        UnexpectedModelBehavior("Exceeded maximum output retries (2)")
    )


def test_exhaustion_errors_carry_the_validation_detail():
    from pydantic_ai.exceptions import UnexpectedModelBehavior

    from marim_harness.runtime.controller import _exhaustion_errors

    exc = UnexpectedModelBehavior("Exceeded maximum output retries (2)")
    exc.__cause__ = _validation_error()
    errors = _exhaustion_errors(exc)
    assert len(errors) == 2
    assert errors[0].startswith("Exceeded maximum output retries")
    assert "not-an-int" in errors[1] or "int" in errors[1]

    assert _exhaustion_errors(UnexpectedModelBehavior("Exceeded maximum output retries (2)")) == [
        "Exceeded maximum output retries (2)"
    ]


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
