"""Structured output crosses the real Claude stream-json transport, without a model call."""

import json

import pytest
from pydantic import BaseModel
from pydantic_ai import Agent, NativeOutput
from pydantic_ai.messages import (
    BinaryContent,
    FunctionToolCallEvent,
    ModelRequest,
    RetryPromptPart,
    UserPromptPart,
)

from marim_harness.claude.env import CLI_BINARY_ENV
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.cli_input import claude_input, prompt_content
from marim_harness.config.external_cli import CliModelError
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_argvs, read_claude_log


class Result(BaseModel):
    value: str


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("with_activity", [False, True])
async def test_native_output_excludes_progress_and_tool_text(
    tmp_path, monkeypatch, streaming, with_activity
):
    monkeypatch.setenv(
        CLI_BINARY_ENV,
        fake_claude_bin(
            tmp_path,
            {
                "turns": [
                    [
                        {"text": "Investigating."},
                        {
                            "tool_use": {
                                "id": "r1",
                                "name": "Read",
                                "input": {"file_path": "proof.txt"},
                            }
                        },
                        {"tool_result": {"id": "r1", "content": "verified"}},
                        {"text": "Finished investigating."},
                        {"result": {"structured_output": {"value": "verified"}}},
                    ]
                ]
            },
        ),
    )
    model = ClaudeCliModel("sonnet")
    model.cwd = str(tmp_path)
    activity = []

    async def collect(events):
        activity.extend(events)

    if with_activity:
        model.on_activity = collect
    agent = Agent(model, output_type=Result, retries=0)
    try:
        if streaming:
            async with agent.run_stream("Read proof.txt") as run:
                output = await run.get_output()
        else:
            output = (await agent.run("Read proof.txt")).output
        assert output == Result(value="verified")
        argv = read_claude_argv(tmp_path)
        schema = json.loads(argv[argv.index("--json-schema") + 1])
        assert schema["properties"]["value"]["type"] == "string"
        assert schema["required"] == ["value"]
        if with_activity:
            assert any(isinstance(event, FunctionToolCallEvent) for event in activity)
    finally:
        await model.aclose()


class Count(BaseModel):
    count: int


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_schema_changes_restart_and_resume_then_restore_plain_text(
    tmp_path, monkeypatch, streaming
):
    monkeypatch.setenv(
        CLI_BINARY_ENV,
        fake_claude_bin(
            tmp_path,
            {
                "known_sessions": ["S1"],
                "turns": [
                    [
                        {"text": "ordinary reply"},
                        {"result": {"structured_output": {"value": "verified", "count": 3}}},
                    ]
                ],
            },
        ),
    )
    model = ClaudeCliModel("sonnet")
    model.cwd = str(tmp_path)

    async def run(output_type, history):
        agent = Agent(model, output_type=output_type)
        if streaming:
            async with agent.run_stream("next", message_history=history) as response:
                return await response.get_output(), response.all_messages()
        response = await agent.run("next", message_history=history)
        return response.output, response.all_messages()

    try:
        output, history = await run(Result, [])
        assert output == Result(value="verified")
        output, history = await run(NativeOutput(Result), history)
        assert output == Result(value="verified")
        assert len(read_claude_argvs(tmp_path)) == 1  # Same schema reuses the process.
        output, history = await run(Count, history)
        assert output == Count(count=3)
        output, _ = await run(str, history)
        assert output == "ordinary reply"
        argvs = read_claude_argvs(tmp_path)
        assert len(argvs) == 3
        assert "count" in json.loads(argvs[1][argvs[1].index("--json-schema") + 1])["required"]
        assert "--json-schema" not in argvs[2]
        assert all(argv[argv.index("--resume") + 1] == "S1" for argv in argvs[1:])
    finally:
        await model.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
@pytest.mark.parametrize("failed", [False, True])
async def test_terminal_json_text_isolated_and_error_result_never_validated(
    tmp_path, monkeypatch, streaming, failed
):
    terminal = {"result": '{"value":"verified"}'}
    if failed:
        terminal.update(is_error=True, subtype="error_max_structured_output_retries")
    monkeypatch.setenv(
        CLI_BINARY_ENV,
        fake_claude_bin(
            tmp_path,
            {
                "turns": [
                    [
                        {"text": "Investigating."},
                        {"result": terminal},
                    ]
                ]
            },
        ),
    )
    model = ClaudeCliModel("sonnet")
    model.cwd = str(tmp_path)
    agent = Agent(model, output_type=Result, retries=0)

    async def run():
        if streaming:
            async with agent.run_stream("read") as response:
                return await response.get_output()
        return (await agent.run("read")).output

    try:
        if failed:
            with pytest.raises(CliModelError, match="error_max_structured_output_retries"):
                await run()
        else:
            assert await run() == Result(value="verified")
    finally:
        await model.aclose()


@pytest.mark.parametrize("history", [False, True])
@pytest.mark.parametrize("image", [False, True])
def test_retry_prompt_preserved_with_text_and_images(history, image):
    content = ["please correct"]
    if image:
        content.append(BinaryContent(data=b"image", media_type="image/png"))
    messages = [
        ModelRequest(
            parts=[
                UserPromptPart(content=content),
                RetryPromptPart(content="value must be a string"),
            ]
        )
    ]
    blocks = claude_input(prompt_content(messages, history=history))
    text = "".join(block.get("text", "") for block in blocks)
    assert "please correct" in text
    assert "value must be a string" in text
    assert sum(block["type"] == "image" for block in blocks) == int(image)


@pytest.mark.anyio
@pytest.mark.parametrize("stay_structured", [False, True])
async def test_model_switch_transfers_process_schema(tmp_path, monkeypatch, stay_structured):
    monkeypatch.setenv(
        CLI_BINARY_ENV,
        fake_claude_bin(
            tmp_path,
            {
                "known_sessions": ["S1"],
                "turns": [[{"text": "plain"}, {"result": {"structured_output": {"value": "ok"}}}]],
            },
        ),
    )
    previous = ClaudeCliModel("sonnet")
    previous.cwd = str(tmp_path)
    replacement = ClaudeCliModel("opus")
    replacement.cwd = str(tmp_path)
    try:
        first = await Agent(previous, output_type=Result).run("first")
        replacement.adopt(previous)
        second = await Agent(replacement, output_type=Result if stay_structured else str).run(
            "next", message_history=first.all_messages()
        )
        assert second.output == (Result(value="ok") if stay_structured else "plain")
        argvs = read_claude_argvs(tmp_path)
        assert len(argvs) == (1 if stay_structured else 2)
        assert ("--json-schema" in argvs[-1]) == stay_structured
    finally:
        await previous.aclose()
        await replacement.aclose()


@pytest.mark.anyio
@pytest.mark.parametrize("streaming", [False, True])
async def test_output_retry_sends_validation_feedback(tmp_path, monkeypatch, streaming):
    monkeypatch.setenv(
        CLI_BINARY_ENV,
        fake_claude_bin(
            tmp_path,
            {
                "turns": [
                    [{"result": {"structured_output": {"value": 123}}}],
                    [{"result": {"structured_output": {"value": "verified"}}}],
                ]
            },
        ),
    )
    model = ClaudeCliModel("sonnet")
    model.cwd = str(tmp_path)

    async def consume(ctx, events):
        async for _ in events:
            pass

    agent = Agent(model, output_type=Result, retries=1)
    try:
        result = await agent.run(
            "Return value verified.", event_stream_handler=consume if streaming else None
        )
        assert result.output == Result(value="verified")
        prompts = [
            "".join(block.get("text", "") for block in obj["message"]["content"])
            for obj in read_claude_log(tmp_path)
            if obj.get("type") == "user"
        ]
        assert len(prompts) == 2
        assert "value" in prompts[1] and "string" in prompts[1]
        assert "Return value verified." not in prompts[1]
    finally:
        await model.aclose()
