"""Per-spawn context masking for sub-agents.

The masker rides every outgoing sub-agent request (a ProcessHistory capability).
Its contract has three parts these tests pin: (1) below the trigger it changes
nothing; (2) crossing the trigger masks stale tool observations in one batch,
sparing the newest keep_recent; (3) between triggers it re-applies EXACTLY the
committed set — a return spared at trigger time stays unmasked even after newer
returns arrive, so the request prefix is byte-stable and the provider prompt
cache survives. A stateless newest-N mask would fail (3).
"""

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.compaction import MASKED_OBSERVATION
from marim_harness.subagents.masking import ObservationMasker
from tests.conftest import _make_deps, _make_harness


def _round(i: int, size: int) -> list:
    """One tool round: the assistant calls tool ``t{i}``; it returns ``size`` chars."""
    return [
        ModelResponse(parts=[
            ToolCallPart(tool_name="read_file", args={}, tool_call_id=f"t{i}")
        ]),
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content="x" * size,
                           tool_call_id=f"t{i}")
        ]),
    ]


def _history(rounds: int, size: int) -> list:
    history: list = [ModelRequest(parts=[UserPromptPart(content="task")])]
    for i in range(rounds):
        history += _round(i, size)
    return history


def _returns(history) -> dict[str, str]:
    """tool_call_id -> content for every ToolReturnPart in ``history``."""
    return {
        p.tool_call_id: str(p.content)
        for m in history
        for p in getattr(m, "parts", [])
        if isinstance(p, ToolReturnPart)
    }


def test_below_trigger_masks_nothing():
    masker = ObservationMasker(max_tokens=100_000)
    history = _history(rounds=3, size=400)
    view = masker.mask(history)
    assert all(c == "x" * 400 for c in _returns(view).values())


def test_crossing_trigger_masks_stale_keeps_recent():
    # trigger = 0.75 * 1000 = 750 tokens; 4 rounds x 1200 chars ≈ 1200 tokens.
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    view = masker.mask(_history(rounds=4, size=1200))
    returns = _returns(view)
    assert returns["t0"] == MASKED_OBSERVATION
    assert returns["t1"] == MASKED_OBSERVATION
    assert returns["t2"] == "x" * 1200
    assert returns["t3"] == "x" * 1200


def test_never_mutates_the_input_history():
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)
    assert all(c == "x" * 1200 for c in _returns(history).values())


def test_mask_set_is_stable_between_triggers():
    """After a trigger, a spared return stays unmasked even once newer returns
    arrive — until the NEXT trigger. This is the cache-stability property; a
    stateless newest-N mask would re-mask t2 here and bust the prefix cache."""
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)                       # trigger 1: masks t0, t1
    history += _round(4, size=200)             # small growth: stays under trigger
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t2"] == "x" * 1200         # spared at trigger 1, STILL spared
    assert returns["t4"] == "x" * 200


def test_second_trigger_extends_the_mask_set():
    masker = ObservationMasker(max_tokens=1000, keep_recent=2, min_chars=100)
    history = _history(rounds=4, size=1200)
    masker.mask(history)                       # trigger 1: masks t0, t1
    history += _round(4, size=1200)            # big growth: crosses trigger again
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t2"] == MASKED_OBSERVATION  # newly stale, masked at trigger 2
    assert returns["t3"] == "x" * 1200          # newest 2 spared
    assert returns["t4"] == "x" * 1200


def test_small_returns_are_never_masked():
    masker = ObservationMasker(max_tokens=1000, keep_recent=1, min_chars=100)
    history = [ModelRequest(parts=[UserPromptPart(content="task")])]
    history += _round(0, size=50)              # tiny: below min_chars
    history += _round(1, size=4000)
    history += _round(2, size=4000)
    view = masker.mask(history)
    returns = _returns(view)
    assert returns["t0"] == "x" * 50           # small stays, masking it buys nothing
    assert returns["t1"] == MASKED_OBSERVATION


@pytest.mark.anyio
async def test_built_subagent_masks_stale_observations_in_requests(tmp_path):
    """End-to-end through SubagentRunner.build: with a tiny context budget, older
    bulky tool returns are masked in the request the model actually sees — and,
    because pydantic-ai writes the processed history back into run state
    (``ctx.state.message_history[:] = messages``), the masking persists into
    ``all_messages()``. The second property is pinned so an upstream semantics
    change is caught here instead of silently altering transcript content."""
    seen: dict = {}
    calls = {"n": 0}

    def fn(messages, info):
        calls["n"] += 1
        if calls["n"] <= 3:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="blob", args={}, tool_call_id=f"t{calls['n']}")])
        seen["messages"] = messages
        return ModelResponse(parts=[TextPart(content="done")])

    deps = _make_deps(tmp_path)
    runner = _make_harness(FunctionModel(fn), deps).subagents
    runner._max_context_tokens = 400   # trigger ≈ 300 tokens; each blob is ~500
    runner._mask_keep_recent = 1
    runner._mask_min_chars = 100
    sub, err = runner.build("general")
    assert err is None, err
    assert sub is not None

    @sub.tool_plain
    def blob() -> str:
        return "x" * 2000

    result = await runner._run_to_completion(sub, "go", deps, None, None)
    assert result.output == "done"

    request_returns = [
        str(p.content) for m in seen["messages"]
        for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    assert MASKED_OBSERVATION in request_returns        # stale observations masked
    assert any("x" * 100 in c for c in request_returns)  # newest spared

    stored_returns = [
        str(p.content) for m in result.all_messages()
        for p in getattr(m, "parts", []) if isinstance(p, ToolReturnPart)
    ]
    # Write-back semantics: the processed (masked) history IS the stored history.
    assert MASKED_OBSERVATION in stored_returns
