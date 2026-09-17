"""Embedding contracts through the real adapter and a scripted app-server."""

import asyncio

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent

from marim_harness import HarnessBuilder
from marim_harness.config.codex_cli_model import CliModelError
from tests.fakes import read_request_log
from tests.test_codex_cli_model import _hello_turn, _model, _usage_turn

pytestmark = pytest.mark.anyio


class Report(BaseModel):
    summary: str


def _report_turn():
    return [
        {
            "notify": "item/agentMessage/delta",
            "params": {"itemId": "progress", "delta": "Reading files."},
        },
        {
            "notify": "item/started",
            "params": {
                "item": {"id": "c1", "type": "commandExecution", "command": "pwd", "cwd": "/w"}
            },
        },
        {
            "notify": "item/completed",
            "params": {
                "item": {
                    "id": "c1",
                    "type": "commandExecution",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "/w",
                }
            },
        },
        *_hello_turn('{"summary":"checked"}'),
    ]


@pytest.mark.parametrize("streaming", [False, True])
async def test_codex_returns_typed_output_without_progress_text(tmp_path, streaming):
    model = _model(tmp_path, {"turns": [_report_turn()]})
    agent = Agent(model, output_type=Report)
    try:
        if streaming:
            async with agent.run_stream("review") as result:
                output = await result.get_output()
                usage = result.usage
        else:
            result = await agent.run("review")
            output, usage = result.output, result.usage
    finally:
        await model.aclose()
    assert output == Report(summary="checked")
    assert (usage.input_tokens, usage.output_tokens, usage.cache_read_tokens) == (12, 5, 3)
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["outputSchema"]["properties"]["summary"]["type"] == "string"
    assert turn["params"]["outputSchema"]["required"] == ["summary"]
    assert turn["params"]["outputSchema"]["additionalProperties"] is False


async def test_builder_validates_codex_structured_output(tmp_path):
    model = _model(tmp_path, {"turns": [_report_turn()]})
    harness = HarnessBuilder(workspace=tmp_path, model=model).with_output_type(Report).build()
    try:
        outcome = await harness.run_turn("review")
    finally:
        await harness.aclose()
    assert outcome.structured_output == Report(summary="checked")


async def test_codex_invalid_output_is_retried_with_schema(tmp_path):
    model = _model(tmp_path, {"turns": [_hello_turn('{"wrong":1}'), _report_turn()]})
    try:
        result = await Agent(model, output_type=Report).run("review")
    finally:
        await model.aclose()
    assert result.output == Report(summary="checked")
    turns = [r for r in read_request_log(tmp_path) if r["method"] == "turn/start"]
    assert len(turns) == 2
    assert all(turn["params"]["outputSchema"]["required"] == ["summary"] for turn in turns)
    assert "summary" in turns[1]["params"]["input"][0]["text"]
    assert "Field required" in turns[1]["params"]["input"][0]["text"]


async def test_structured_activity_still_reaches_callback(tmp_path):
    model = _model(tmp_path, {"turns": [_report_turn()]})
    activity = []

    async def record(events):
        activity.extend(events)

    model.on_activity = record
    try:
        result = await Agent(model, output_type=Report).run("review")
    finally:
        await model.aclose()
    assert result.output == Report(summary="checked")
    assert len(activity) == 2
    assert activity[0].part.tool_name == "bash"
    assert activity[0].part.args["command"] == "pwd"
    assert activity[1].part.content == "/w"


async def test_failed_turn_preserves_observed_usage_once(tmp_path):
    model = _model(
        tmp_path, {"turns": [[*_hello_turn(), {"fail": "provider down"}], _report_turn()]}
    )
    try:
        with pytest.raises(CliModelError, match="provider down"):
            await Agent(model, output_type=Report).run("review")
        assert (
            model.observed_usage.requests,
            model.observed_usage.input_tokens,
            model.observed_usage.output_tokens,
        ) == (1, 12, 5)
        # The next thread-total is unchanged in this fixture: no double count.
        result = await Agent(model, output_type=Report).run("retry")
        assert result.output == Report(summary="checked")
        assert (
            model.observed_usage.requests,
            model.observed_usage.input_tokens,
            model.observed_usage.output_tokens,
        ) == (2, 12, 5)
    finally:
        await model.aclose()


async def test_cancelled_turn_preserves_observed_usage(tmp_path):
    counts = {"inputTokens": 12, "outputTokens": 5, "cachedInputTokens": 3}
    model = _model(tmp_path, {"turns": [_usage_turn("working", counts, counts) + [{"hang": True}]]})
    task = asyncio.create_task(Agent(model, output_type=Report).run("review"))
    try:

        async def wait_for_usage():
            while model.context_report is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_for_usage(), 5)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        assert (
            model.observed_usage.requests,
            model.observed_usage.input_tokens,
            model.observed_usage.output_tokens,
            model.observed_usage.cache_read_tokens,
        ) == (1, 12, 5, 3)
    finally:
        if not task.done():
            task.cancel()
        await model.aclose()
