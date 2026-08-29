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
