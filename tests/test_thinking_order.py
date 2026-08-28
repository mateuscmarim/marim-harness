"""Reasoning must render *above* the reply it produced — live and on replay.

Stored/streamed part order is not always causal order. deepseek-v4 via OpenRouter
opens the reply with a whitespace-only content delta before the first reasoning
delta; pydantic-ai only skips leading whitespace for R1 (``deepseek.py``:
``ignore_streamed_leading_whitespace=is_r1``), so a lone " " is truthy and starts
the TextPart *first*. Every response then persists as ``[text, thinking]`` and the
answer used to mount above the thought that produced it.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
)

from marim_harness.interfaces.tui.session_view import order_response_parts
from marim_harness.interfaces.tui.widgets import AssistantMessage, ThinkingWidget
from tests.conftest import _make_deps


def _app(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.interfaces.tui.app import HarnessApp
    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")
    return HarnessApp(harness)


def _kinds(children):
    """The transcript's thinking/text widgets, in DOM order."""
    return [
        "thinking" if isinstance(w, ThinkingWidget) else "text"
        for w in children
        if isinstance(w, (ThinkingWidget, AssistantMessage))
    ]


# --- the pure replay reordering ------------------------------------------------


def test_order_response_parts_hoists_thinking_above_a_blank_opened_reply():
    # The persisted deepseek shape: the reply's leading whitespace is the
    # fingerprint of the blank content delta that opened the TextPart first.
    parts = [TextPart(content=" Claro!"), ThinkingPart(content="The user asks...")]
    assert [type(p) for p in order_response_parts(parts)] == [ThinkingPart, TextPart]


def test_order_response_parts_leaves_genuine_text_first_alone():
    """The fidelity guarantee: text that is real from its first character truly
    preceded the thought, so replay must keep it there — hoisting would both
    misrepresent the model and disagree with the live path, which mounts a
    content-bearing text part immediately."""
    parts = [TextPart(content="Hello"), ThinkingPart(content="second thought")]
    assert order_response_parts(parts) == parts


def test_order_response_parts_leaves_correct_order_alone():
    parts = [ThinkingPart(content="hmm"), TextPart(content="hi")]
    assert order_response_parts(parts) == parts


def test_order_response_parts_keeps_post_tool_thinking_with_its_own_reply():
    # Tool calls anchor segments: the second thought belongs to the second reply
    # and must not be hoisted to the top of the message, even though both
    # segments carry the blank-opener fingerprint.
    call = ToolCallPart(tool_name="read_file", args={}, tool_call_id="t1")
    parts = [
        ThinkingPart(content="first thought"),
        TextPart(content=" first reply"),
        call,
        TextPart(content=" second reply"),
        ThinkingPart(content="second thought"),
    ]
    assert [
        p.content if not isinstance(p, ToolCallPart) else "CALL"
        for p in order_response_parts(parts)
    ] == ["first thought", " first reply", "CALL", "second thought", " second reply"]


def test_order_response_parts_is_pure():
    parts = [TextPart(content="hi"), ThinkingPart(content="hmm")]
    before = list(parts)
    order_response_parts(parts)
    assert parts == before


# --- the live streaming path ---------------------------------------------------


@pytest.mark.anyio
async def test_blank_text_part_does_not_claim_the_slot_above_thinking(tmp_path):
    """The regression: a whitespace-only TextPart opening the stream must not mount
    an empty message above the thinking block that follows it."""
    from marim_harness.interfaces.tui.stream_render import _TopLevelSink

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        log = app.query_one("#log")
        sink = _TopLevelSink(r, log)

        # Exactly the deepseek-v4 stream shape.
        for event in (
            PartStartEvent(index=0, part=TextPart(content=" ")),
            PartStartEvent(index=1, part=ThinkingPart(content="")),
            PartDeltaEvent(index=1, delta=ThinkingPartDelta(content_delta="The user asks...")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="Claro! Vou te ajudar")),
        ):
            await r.dispatch_stream_event(event, sink)
        await pilot.pause()

        # Reasoning above the reply, and exactly one of each (no stray empty message).
        assert _kinds(log.children) == ["thinking", "text"]

        # The leading whitespace is buffered, not dropped — it still prefixes the reply.
        msg = next(w for w in log.children if isinstance(w, AssistantMessage))
        assert msg.text == " Claro! Vou te ajudar"


@pytest.mark.anyio
async def test_normal_thinking_then_text_order_is_unchanged(tmp_path):
    """Providers that already stream reasoning first keep working."""
    from marim_harness.interfaces.tui.stream_render import _TopLevelSink

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        log = app.query_one("#log")
        sink = _TopLevelSink(r, log)

        for event in (
            PartStartEvent(index=0, part=ThinkingPart(content="hmm")),
            PartStartEvent(index=1, part=TextPart(content="hello")),
            PartDeltaEvent(index=1, delta=TextPartDelta(content_delta=" there")),
        ):
            await r.dispatch_stream_event(event, sink)
        await pilot.pause()

        assert _kinds(log.children) == ["thinking", "text"]
        msg = next(w for w in log.children if isinstance(w, AssistantMessage))
        assert msg.text == "hello there"


@pytest.mark.anyio
async def test_whitespace_only_reply_mounts_nothing(tmp_path):
    """A reply that never gains visible content leaves no empty bubble behind —
    matching ThinkingWidget, which drops an empty thought rather than showing a
    bare label."""
    from marim_harness.interfaces.tui.stream_render import _TopLevelSink

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        log = app.query_one("#log")
        sink = _TopLevelSink(r, log)

        for event in (
            PartStartEvent(index=0, part=TextPart(content=" ")),
            PartDeltaEvent(index=0, delta=TextPartDelta(content_delta="  ")),
        ):
            await r.dispatch_stream_event(event, sink)
        await pilot.pause()

        assert _kinds(log.children) == []


@pytest.mark.anyio
async def test_genuine_text_first_agrees_between_live_and_replay(tmp_path):
    """The two paths must not disagree. A reply that is real from its first
    character mounts immediately live, and ``order_response_parts`` leaves the same
    stored shape alone — so both render [text, thinking]."""
    from marim_harness.interfaces.tui.stream_render import _TopLevelSink

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        r = app.stream
        log = app.query_one("#log")
        sink = _TopLevelSink(r, log)

        for event in (
            PartStartEvent(index=0, part=TextPart(content="Hello")),
            PartStartEvent(index=1, part=ThinkingPart(content="afterthought")),
        ):
            await r.dispatch_stream_event(event, sink)
        await pilot.pause()
        live = _kinds(log.children)

        replayed = order_response_parts(
            [TextPart(content="Hello"), ThinkingPart(content="afterthought")]
        )
        assert live == ["text", "thinking"]
        assert [
            "thinking" if isinstance(p, ThinkingPart) else "text" for p in replayed
        ] == live


@pytest.mark.anyio
async def test_replay_renders_thinking_above_text(tmp_path):
    """Replaying a persisted ``[text, thinking]`` response matches the live order."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        log = app.query_one("#log")
        app.harness.session.history.append(
            ModelResponse(
                parts=[
                    TextPart(content=" Claro! Vou te ajudar"),
                    ThinkingPart(content="The user asks..."),
                ]
            )
        )
        await app.session.replay_history(log)
        await pilot.pause()

        assert _kinds(log.children) == ["thinking", "text"]
