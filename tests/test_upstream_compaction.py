import pytest
from pydantic_ai.messages import (
    ModelMessagesTypeAdapter,
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    ToolSearchReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai_harness.compaction import ClearToolResults, compact_now


def tool_history():
    history = [ModelRequest(parts=[UserPromptPart("Inspect the project.")])]
    for index in range(5):
        call_id = str(index)
        history.extend(
            [
                ModelResponse(
                    parts=[ToolCallPart("read_file", {"path": call_id}, tool_call_id=call_id)]
                ),
                ModelRequest(parts=[ToolReturnPart("read_file", "x" * 4000, tool_call_id=call_id)]),
            ]
        )
    return history


def _returns(history):
    return [
        part.content
        for message in history
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


def _assert_serializable(history):
    encoded = ModelMessagesTypeAdapter.dump_json(history)
    assert ModelMessagesTypeAdapter.validate_json(encoded) == history


@pytest.mark.anyio
async def test_clear_preserves_recent_results_and_serializable_history():
    history = tool_history()
    original = ModelMessagesTypeAdapter.dump_json(history)
    cleared = await compact_now(
        ClearToolResults(max_tokens=1, keep_pairs=2), history, model=TestModel()
    )
    returns = _returns(cleared)
    assert returns[-2:] == ["x" * 4000, "x" * 4000]
    assert all(len(content) < 4000 for content in returns[:-2])
    assert ModelMessagesTypeAdapter.dump_json(history) == original
    _assert_serializable(cleared)


@pytest.mark.anyio
@pytest.mark.xfail(
    strict=True,
    reason=(
        "pydantic-ai-harness 0.31.0 groups pairs by tool_call_id globally and clears the "
        "newest result when an ID is reused"
    ),
)
async def test_clear_preserves_recent_result_when_call_ids_repeat():
    history = [ModelRequest(parts=[UserPromptPart("Inspect twice.")])]
    contents = [f"round-{index}:" + "x" * 4000 for index in range(3)]
    for content in contents:
        history.extend(
            [
                ModelResponse(
                    parts=[ToolCallPart("read_file", {"path": content[:7]}, tool_call_id="same")]
                ),
                ModelRequest(parts=[ToolReturnPart("read_file", content, tool_call_id="same")]),
            ]
        )

    cleared = await compact_now(
        ClearToolResults(max_tokens=1, keep_pairs=1), history, model=TestModel()
    )

    returns = _returns(cleared)
    assert returns[-1] == contents[-1]
    assert all(len(content) < len(contents[0]) for content in returns[:-1])
    _assert_serializable(cleared)


@pytest.mark.anyio
async def test_clear_preserves_recent_results_from_parallel_round():
    history = [ModelRequest(parts=[UserPromptPart("Inspect in parallel.")])]
    contents = [f"parallel-{index}:" + "x" * 4000 for index in range(3)]
    history.extend(
        [
            ModelResponse(
                parts=[
                    ToolCallPart("read_file", {"path": str(index)}, tool_call_id=str(index))
                    for index in range(3)
                ]
            ),
            ModelRequest(
                parts=[
                    ToolReturnPart("read_file", content, tool_call_id=str(index))
                    for index, content in enumerate(contents)
                ]
            ),
        ]
    )

    cleared = await compact_now(
        ClearToolResults(max_tokens=1, keep_pairs=2), history, model=TestModel()
    )

    returns = _returns(cleared)
    assert len(returns[0]) < len(contents[0])
    assert returns[-2:] == contents[-2:]
    _assert_serializable(cleared)


@pytest.mark.anyio
async def test_clear_keeps_typed_framework_tool_results_valid():
    payload = {"discovered_tools": [{"name": "example-" + "d" * 4000}]}
    history = [
        ModelRequest(parts=[UserPromptPart("Discover tools.")]),
        ModelResponse(parts=[ToolCallPart("search_tools", {}, tool_call_id="search")]),
        ModelRequest(parts=[ToolSearchReturnPart(content=payload, tool_call_id="search")]),
    ]

    cleared = await compact_now(
        ClearToolResults(max_tokens=1, keep_pairs=0), history, model=TestModel()
    )

    typed_return = cleared[-1].parts[0]
    assert isinstance(typed_return, ToolSearchReturnPart)
    assert typed_return.content == payload
    _assert_serializable(cleared)
