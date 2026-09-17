"""Native sub-agent request-time clearing through upstream capabilities."""

import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.function import FunctionModel
from pydantic_ai_harness.compaction import ClearToolResults

from marim_harness.subagents.policies import MaskingPolicy
from tests.conftest import _make_deps, _make_harness, _text_model

CLEARED = ClearToolResults(max_tokens=1).placeholder


def _returns(history: list) -> list[str]:
    return [
        str(part.content)
        for message in history
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def test_policy_builds_a_fresh_upstream_clearer_per_spawn():
    policy = MaskingPolicy(keep_recent=2)
    first = policy.clearer(750)
    second = policy.clearer(750)
    assert first is not second
    assert first is not None
    assert first.max_tokens == 750
    assert first.keep_pairs == 2
    assert first.clear_tool_inputs is False


def test_policy_disables_request_time_clearing():
    assert MaskingPolicy(enabled=False).clearer(1) is None


async def _run_tool_sequence(
    tmp_path, *, rounds: int, keep_recent: int, checkpoint=None
) -> tuple[list, object]:
    requests: list[list] = []
    calls = {"count": 0}

    def fn(messages, info):
        requests.append(
            ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(messages))
        )
        calls["count"] += 1
        if calls["count"] <= rounds:
            return ModelResponse(
                parts=[ToolCallPart("blob", {}, tool_call_id=f"t{calls['count']}")]
            )
        return ModelResponse(parts=[TextPart("done")])

    deps = _make_deps(tmp_path)
    runner = _make_harness(FunctionModel(fn), deps).subagents
    runner._masking = MaskingPolicy(keep_recent=keep_recent)
    sub, error = runner.build("general", mask_trigger=1, checkpoint=checkpoint)
    assert error is None
    assert sub is not None

    @sub.tool_plain
    def blob() -> str:
        return "x" * 4000

    result = await runner._driver.run_to_completion(sub, "go", deps, None, None)
    return requests, result


@pytest.mark.anyio
async def test_built_subagent_clears_stale_results_and_preserves_recent_result(tmp_path):
    requests, result = await _run_tool_sequence(tmp_path, rounds=3, keep_recent=1)
    final_returns = _returns(requests[-1])
    assert final_returns[:-1] == [CLEARED, CLEARED]
    assert final_returns[-1] == "x" * 4000
    assert _returns(result.all_messages()) == final_returns


@pytest.mark.anyio
async def test_cleared_prefix_stays_stable_across_consecutive_requests(tmp_path):
    requests, _ = await _run_tool_sequence(tmp_path, rounds=4, keep_recent=2)
    assert _returns(requests[3])[:1] == [CLEARED]
    assert _returns(requests[4])[:1] == [CLEARED]


@pytest.mark.anyio
async def test_checkpoint_receives_sanitized_reduced_history(tmp_path):
    checkpoints: list[list] = []

    def checkpoint(messages: list) -> None:
        checkpoints.append(
            ModelMessagesTypeAdapter.validate_json(ModelMessagesTypeAdapter.dump_json(messages))
        )

    requests, _ = await _run_tool_sequence(tmp_path, rounds=3, keep_recent=1, checkpoint=checkpoint)
    assert _returns(checkpoints[-1]) == _returns(requests[-1])
    assert _returns(checkpoints[-1])[:-1] == [CLEARED, CLEARED]


@pytest.mark.anyio
async def test_duplicate_tool_ids_keep_all_results_and_round_trip():
    history = [ModelRequest(parts=[UserPromptPart("task")])]
    for call_id in ("same", "same", "unique"):
        history.extend(
            [
                ModelResponse(parts=[ToolCallPart("read_file", {}, tool_call_id=call_id)]),
                ModelRequest(parts=[ToolReturnPart("read_file", "x" * 4000, tool_call_id=call_id)]),
            ]
        )
    clearer = MaskingPolicy(keep_recent=1).clearer(1)
    assert clearer is not None

    from pydantic_ai.models.test import TestModel
    from pydantic_ai_harness.compaction import compact_now

    reduced = await compact_now(clearer, history, model=TestModel())
    assert _returns(reduced)[:2] == ["x" * 4000, "x" * 4000]
    encoded = ModelMessagesTypeAdapter.dump_json(reduced)
    assert ModelMessagesTypeAdapter.validate_json(encoded) == reduced


@pytest.mark.anyio
async def test_parallel_tool_results_keep_the_recent_round_together():
    history = [ModelRequest(parts=[UserPromptPart("task")])]
    history.extend(
        [
            ModelResponse(
                parts=[
                    ToolCallPart("read_file", {}, tool_call_id="old"),
                    ToolCallPart("read_file", {}, tool_call_id="recent-a"),
                    ToolCallPart("read_file", {}, tool_call_id="recent-b"),
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart("read_file", "x" * 4000, tool_call_id="old"),
                    ToolReturnPart("read_file", "a" * 4000, tool_call_id="recent-a"),
                    ToolReturnPart("read_file", "b" * 4000, tool_call_id="recent-b"),
                ]
            ),
        ]
    )
    clearer = MaskingPolicy(keep_recent=2).clearer(1)
    assert clearer is not None

    from pydantic_ai.models.test import TestModel
    from pydantic_ai_harness.compaction import compact_now

    reduced = await compact_now(clearer, history, model=TestModel())
    assert _returns(reduced) == [CLEARED, "a" * 4000, "b" * 4000]


@pytest.mark.anyio
async def test_spawn_trigger_follows_its_loaded_model_window(tmp_path):
    from marim_harness.config.context_limits import ContextLimits

    async def fake_local():
        return {"small": 10_000, "large": 100_000}

    runner = _make_harness(_text_model(), _make_deps(tmp_path)).subagents
    runner._masking = MaskingPolicy(limits=ContextLimits(budget=180_000, fetchers=[fake_local]))
    assert await runner._mask_trigger_for("small") == 8_000
    assert await runner._mask_trigger_for("large") == 80_000
