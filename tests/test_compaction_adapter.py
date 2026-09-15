import asyncio
from copy import deepcopy

import pytest
from pydantic_ai.exceptions import UsageLimitExceeded
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models.test import TestModel
from pydantic_ai.usage import RunUsage, UsageLimits
from pydantic_ai_harness.compaction import SummarizingCompaction, compact_now

from marim_harness.session.compaction import reduce_history, safe_tool_result_clearer


def _repeated_id_history(tool_name: str = "read_file") -> list[ModelRequest | ModelResponse]:
    history: list[ModelRequest | ModelResponse] = [
        ModelRequest(parts=[UserPromptPart("Inspect twice.")])
    ]
    for index in range(3):
        history.extend(
            [
                ModelResponse(
                    parts=[ToolCallPart(tool_name, {"index": index}, tool_call_id="reused")]
                ),
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name, f"round-{index}:" + "x" * 4000, tool_call_id="reused"
                        )
                    ]
                ),
            ]
        )
    return history


def _return_contents(messages):
    return [
        part.content
        for message in messages
        for part in message.parts
        if isinstance(part, ToolReturnPart)
    ]


@pytest.mark.anyio
async def test_safe_clearer_skips_tool_name_with_reused_call_id(caplog):
    history = _repeated_id_history()

    clearer = safe_tool_result_clearer(keep_pairs=1, max_tokens=1)
    result = await compact_now(clearer, history, model=TestModel())

    assert result == history
    assert "duplicate tool call IDs" in caplog.text
    assert "round-0" not in caplog.text


@pytest.mark.anyio
@pytest.mark.parametrize("tool_name", ["bash", "write_file", "edit_file", "run_workflow"])
async def test_safe_clearer_never_clears_mutating_tool_results(tool_name):
    history = _repeated_id_history(tool_name)

    clearer = safe_tool_result_clearer(keep_pairs=0, max_tokens=1)
    result = await compact_now(clearer, history, model=TestModel())

    assert result == history


@pytest.mark.anyio
async def test_safe_clearer_delegates_normal_clearing_without_mutating_input():
    history = _repeated_id_history()
    for index, message in enumerate(history[1:]):
        for part in message.parts:
            if isinstance(part, (ToolCallPart, ToolReturnPart)):
                part.tool_call_id = str(index // 2)
    original = deepcopy(history)

    result = await compact_now(
        safe_tool_result_clearer(keep_pairs=1, max_tokens=1),
        history,
        model=TestModel(),
        usage=RunUsage(),
    )

    assert history == original
    returns = _return_contents(result)
    assert returns[-1].startswith("round-2:")
    assert returns[:-1] == ["[tool result cleared]", "[tool result cleared]"]


@pytest.mark.anyio
async def test_automatic_reduction_does_nothing_below_budget():
    history = _repeated_id_history()
    usage = RunUsage()

    result = await reduce_history(
        history,
        model=TestModel(custom_output_text="Retained task summary"),
        summary=SummarizingCompaction(max_tokens=1, keep_messages=3),
        target_tokens=1_000_000,
        keep_messages=3,
        keep_pairs=1,
        clear=True,
        force=False,
        focus=None,
        usage=usage,
    )

    assert result.messages == history
    assert result.stages == ()
    assert not result.restructured
    assert usage.requests == 0


@pytest.mark.anyio
async def test_clearing_that_reaches_target_skips_summary():
    history = _repeated_id_history()
    for index, message in enumerate(history[1:]):
        for part in message.parts:
            if isinstance(part, (ToolCallPart, ToolReturnPart)):
                part.tool_call_id = str(index // 2)
    usage = RunUsage()

    result = await reduce_history(
        history,
        model=TestModel(custom_output_text="Retained task summary"),
        summary=SummarizingCompaction(max_tokens=1, keep_messages=3),
        target_tokens=1_500,
        keep_messages=3,
        keep_pairs=1,
        clear=True,
        force=False,
        focus=None,
        usage=usage,
    )

    assert result.stages == ("clear",)
    assert not result.restructured
    assert usage.requests == 0


@pytest.mark.anyio
async def test_forced_summary_runs_below_automatic_threshold():
    usage = RunUsage()

    result = await reduce_history(
        _repeated_id_history(),
        model=TestModel(custom_output_text="Retained task summary"),
        summary=SummarizingCompaction(max_tokens=1, keep_messages=3),
        target_tokens=1_000_000,
        keep_messages=3,
        keep_pairs=1,
        clear=False,
        force=True,
        focus="Keep the authentication requirement",
        usage=usage,
    )

    assert result.restructured
    assert result.stages == ("summary",)
    assert usage.requests == 1


@pytest.mark.anyio
async def test_spent_request_budget_escapes_without_trimming_history():
    history = _repeated_id_history()
    original = deepcopy(history)
    usage = RunUsage(requests=1)

    with pytest.raises(UsageLimitExceeded):
        await reduce_history(
            history,
            model=TestModel(custom_output_text="Retained task summary"),
            summary=SummarizingCompaction(max_tokens=1, keep_messages=3),
            target_tokens=1,
            keep_messages=3,
            keep_pairs=1,
            clear=False,
            force=True,
            focus=None,
            usage=usage,
            usage_limits=UsageLimits(request_limit=1),
        )

    assert history == original


class _CancelledSummary:
    async def compact(self, messages, ctx):
        raise asyncio.CancelledError


@pytest.mark.anyio
async def test_cancellation_escapes_without_trimming_history():
    history = _repeated_id_history()
    original = deepcopy(history)

    with pytest.raises(asyncio.CancelledError):
        await reduce_history(
            history,
            model=TestModel(),
            summary=_CancelledSummary(),
            target_tokens=1,
            keep_messages=3,
            keep_pairs=1,
            clear=False,
            force=True,
            focus=None,
            usage=RunUsage(),
        )

    assert history == original
