"""Image inputs through the real adapters and their subprocess transports."""

from __future__ import annotations

import asyncio
import base64

import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from marim_harness.codex.server import CodexServer
from marim_harness.config.claude_cli_model import ClaudeCliModel
from marim_harness.config.cli_input import claude_input, codex_input, prompt_content
from marim_harness.config.codex_cli_model import CodexCliModel
from marim_harness.config.external_cli import CliModelError
from tests.conftest import _make_deps, _make_harness, _text_model
from tests.fakes import fake_claude_bin, fake_codex_bin, read_claude_log, read_request_log

pytestmark = pytest.mark.anyio
PARAMS = ModelRequestParameters()
FIRST = BinaryContent(data=b"\x89PNG\xfb\xff", media_type="image/png")
SECOND = BinaryContent(data=b"\xff\xd8\xff", media_type="image/jpeg")


@pytest.fixture(params=["claude", "codex"])
def backend(request):
    return request.param


def _model(backend, tmp_path, monkeypatch, *, resume=False, waiting=False):
    if backend == "claude":
        turn = [{"text": "ready"}]
        if waiting:
            turn.append({"await_user": True})
        binary = fake_claude_bin(
            tmp_path, {"known_sessions": ["OLD"] if resume else [], "turns": [turn]}
        )
        monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", binary)
        model = ClaudeCliModel("sonnet")
    else:
        turn = [{"notify": "item/agentMessage/delta", "params": {"delta": "ready"}}]
        if waiting:
            turn.append({"hang": True})
        binary = fake_codex_bin(
            tmp_path, {"resumable": ["OLD"] if resume else [], "turns": [turn]}
        )
        model = CodexCliModel("test-model", server=CodexServer(binary=binary))
    model.cwd = str(tmp_path)
    model.mode_getter = lambda: "auto"
    return model


def _inputs(backend, tmp_path):
    if backend == "claude":
        return [
            obj["message"]["content"]
            for obj in read_claude_log(tmp_path)
            if obj.get("type") == "user"
        ]
    return [
        obj["params"]["input"]
        for obj in read_request_log(tmp_path)
        if obj.get("method") in {"turn/start", "turn/steer"}
    ]


def _images(blocks):
    images = []
    for block in blocks:
        if block["type"] != "image":
            continue
        if "source" in block:
            source = block["source"]
            assert source["type"] == "base64"
            media_type, data = source["media_type"], source["data"]
        else:
            header, data = block["url"].split(",", 1)
            assert header.startswith("data:") and header.endswith(";base64")
            media_type = header[5:-7]
        images.append((base64.b64decode(data, validate=True), media_type))
    return images


def _request(text, *images):
    return ModelRequest(parts=[UserPromptPart(content=[text, *images])])


async def _run(model, messages, streaming):
    if not streaming:
        return await model.request(messages, None, PARAMS)
    async with model.request_stream(messages, None, PARAMS) as stream:
        async for _ in stream:
            pass
        return stream.get()


@pytest.mark.parametrize("streaming", [False, True])
async def test_images_reach_cli_and_live_turn_does_not_resend_old_images(
    backend, tmp_path, monkeypatch, streaming
):
    model = _model(backend, tmp_path, monkeypatch)
    first = _request("describe first", FIRST, SECOND)
    try:
        response = await _run(model, [first], streaming)
        await _run(model, [first, response, _request("describe second", SECOND)], streaming)
    finally:
        await model.aclose()
    inputs = _inputs(backend, tmp_path)
    assert _images(inputs[0]) == [(FIRST.data, FIRST.media_type), (SECOND.data, SECOND.media_type)]
    assert _images(inputs[1]) == [(SECOND.data, SECOND.media_type)]
    assert "describe first" not in str(inputs[1])


@pytest.mark.parametrize("resume", [False, True])
async def test_restoration_replays_images_only_when_cli_session_is_missing(
    backend, tmp_path, monkeypatch, resume
):
    model = _model(backend, tmp_path, monkeypatch, resume=resume)
    model.session_ref_getter = lambda: f"{backend}-cli:OLD"
    history = [
        _request("first image", FIRST),
        ModelResponse(parts=[TextPart(content="first answer")]),
        _request("second image", SECOND),
    ]
    try:
        await _run(model, history, streaming=True)
    finally:
        await model.aclose()
    blocks = _inputs(backend, tmp_path)[-1]
    expected = [(SECOND.data, SECOND.media_type)]
    if not resume:
        expected.insert(0, (FIRST.data, FIRST.media_type))
        # The old image remains before its response and the next user's text.
        kinds = [b["type"] for b in blocks]
        first_image = kinds.index("image")
        answer = next(i for i, b in enumerate(blocks) if "first answer" in b.get("text", ""))
        assert first_image < answer < len(blocks) - 1
    assert _images(blocks) == expected


async def test_image_only_input(backend, tmp_path, monkeypatch):
    model = _model(backend, tmp_path, monkeypatch)
    try:
        await _run(model, [ModelRequest(parts=[UserPromptPart(content=[FIRST])])], False)
    finally:
        await model.aclose()
    assert _images(_inputs(backend, tmp_path)[0]) == [(FIRST.data, FIRST.media_type)]


async def test_harness_steers_image_into_open_cli_turn(backend, tmp_path, monkeypatch):
    model = _model(backend, tmp_path, monkeypatch, waiting=True)
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    harness.current_model = model
    task = asyncio.create_task(_run(model, [_request("start")], True))
    try:
        # Wait for the recorded request and its acknowledgement, not a fixed delay.
        async def wait_open():
            while not _inputs(backend, tmp_path):
                await asyncio.sleep(0.01)
            while backend == "codex" and model.thread.current_turn_id is None:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_open(), 5)
        harness.steer("look here", [(SECOND.data, SECOND.media_type)])

        async def wait_steer():
            while len(_inputs(backend, tmp_path)) < 2:
                await asyncio.sleep(0.01)

        await asyncio.wait_for(wait_steer(), 5)
        assert harness.take_buffered_steers() == []
        assert _images(_inputs(backend, tmp_path)[1]) == [(SECOND.data, SECOND.media_type)]
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        await model.aclose()


async def test_image_steer_without_live_turn_stays_buffered(backend, tmp_path, monkeypatch):
    model = _model(backend, tmp_path, monkeypatch)
    harness = _make_harness(_text_model(), _make_deps(tmp_path))
    harness.current_model = model
    attachments = [(FIRST.data, FIRST.media_type)]
    harness.steer("later", attachments)
    assert harness.take_buffered_steers() == [("later", attachments)]
    assert _inputs(backend, tmp_path) == []


@pytest.mark.parametrize("encode", [claude_input, codex_input])
def test_non_image_binary_input_fails_instead_of_disappearing(encode):
    with pytest.raises(CliModelError, match="image attachments"):
        encode([BinaryContent(data=b"audio", media_type="audio/wav")])


def test_prompt_content_keeps_interleaved_text_and_images_in_order():
    content = ["before", FIRST, "between", SECOND, "after"]
    messages = [ModelRequest(parts=[UserPromptPart(content=content)])]
    assert prompt_content(messages, history=False) == content
    assert prompt_content(messages, history=True) == ["User: ", *content, "\n\n"]


def test_latest_request_skips_tool_return_only_requests():
    from pydantic_ai.messages import ToolReturnPart

    messages = [
        _request("look", FIRST),
        ModelRequest(parts=[ToolReturnPart(tool_name="read_file", content="result")]),
    ]
    assert prompt_content(messages, history=False) == ["look", FIRST]
