from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _harness(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(),
        Deps(workspace_root=tmp_path, mode=Mode.auto), instructions="test",
    )


class _FakeCtx:
    def __init__(self):
        self.calls = []

    def enqueue(self, *content, priority="asap"):
        self.calls.append((content, priority))


def test_steer_enqueues_on_active_ctx(tmp_path):
    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h._active_run_ctx = ctx
    h.steer("go left")
    assert ctx.calls == [(("go left",), "asap")]
    assert h._steer_buffer == []  # flushed


def test_steer_with_attachments_enqueues_binary_content(tmp_path):
    from pydantic_ai import BinaryContent

    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h._active_run_ctx = ctx
    h.steer("look", attachments=[(b"\x89PNG", "image/png")])
    content, priority = ctx.calls[0]
    assert content[0] == "look"
    assert isinstance(content[1], BinaryContent)
    assert content[1].media_type == "image/png"
    assert priority == "asap"


def test_steer_buffers_when_no_active_ctx(tmp_path):
    h = _harness(tmp_path)
    assert h._active_run_ctx is None
    h.steer("later")
    assert h._steer_buffer == [("later", None)]


def test_take_buffered_steers_returns_and_clears(tmp_path):
    h = _harness(tmp_path)
    h.steer("a")
    h.steer("b")
    assert h.take_buffered_steers() == [("a", None), ("b", None)]
    assert h._steer_buffer == []


import asyncio


def _recording_streaming_harness(tmp_path, calls):
    from collections.abc import AsyncIterator

    from pydantic_ai.models.function import (
        AgentInfo, DeltaToolCall, FunctionModel,
    )
    from pydantic_ai.messages import ModelMessage

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    async def stream_fn(messages: list[ModelMessage], info: AgentInfo) -> AsyncIterator:
        seen = []
        for m in messages:
            for p in getattr(m, "parts", []):
                seen.append(getattr(p, "content", getattr(p, "tool_name", None)))
        calls.append(seen)
        if len(calls) == 1:
            yield {0: DeltaToolCall(name="slow", json_args="{}", tool_call_id="c1")}
        else:
            yield "done"

    h = Harness(
        FunctionModel(stream_function=stream_fn), BuiltinToolProvider(),
        Deps(workspace_root=tmp_path, mode=Mode.auto), instructions="test",
    )

    @h.agent.tool_plain
    async def slow() -> str:
        await asyncio.sleep(0.3)  # window for a concurrent steer
        return "pong"

    return h


@pytest.mark.anyio
async def test_steer_reaches_a_later_model_request(tmp_path):
    calls: list[list] = []
    h = _recording_streaming_harness(tmp_path, calls)

    async def steerer():
        for _ in range(200):
            if h._active_run_ctx is not None:
                break
            await asyncio.sleep(0.01)
        h.steer("STEER NOW")

    # a no-op event handler so streaming is on and the ctx is captured
    async def handler(ctx, events):
        async for _ in events:
            pass

    out, _ = await asyncio.gather(
        h.run_turn("hello", event_stream_handler=handler),
        steerer(),
    )
    assert out == "done"
    flat = [str(c) for c in calls]
    assert any("STEER NOW" in c for c in flat), f"steer not injected: {calls}"
