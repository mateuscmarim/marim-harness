import asyncio
from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.queue import QueuedMessage
from marim_harness.interfaces.tui.widgets.prompt import PromptInput
from marim_harness.permissions import Mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _noop():
    return None


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


@pytest.mark.anyio
async def test_alt_enter_posts_steer_message(tmp_path):
    from textual.app import App, ComposeResult

    posted = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
            posted.append(event.value)

    app = _App()
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        pi.text = "steer this"
        await pilot.press("alt+enter")
        await pilot.pause()
    assert posted == ["steer this"]


def _tui_app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps,
                      instructions="test")
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_steer_while_busy_calls_harness_steer(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = []
        app.harness.steer = lambda text, attachments=None: seen.append((text, attachments))
        app._turn_worker = object()  # simulate a running turn
        await app.on_prompt_input_steer(PromptInput.Steer("redirect", []))
        assert seen == [("redirect", [])]
        assert app._queue == []  # not queued
        assert app._turn_worker is not None  # no new worker


@pytest.mark.anyio
async def test_steer_while_idle_runs_normally(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []
        app._start_turn = lambda text, attachments=None: started.append(text) or _noop()
        app._turn_worker = None
        await app.on_prompt_input_steer(PromptInput.Steer("just run", []))
        assert started == ["just run"]


@pytest.mark.anyio
async def test_empty_steer_is_noop(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        seen = []
        app.harness.steer = lambda *a, **k: seen.append(a)
        app._turn_worker = object()
        await app.on_prompt_input_steer(PromptInput.Steer("   ", []))
        assert seen == []  # empty text, no attachments -> no-op


@pytest.mark.anyio
async def test_stranded_steer_goes_to_front_of_queue(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("existing", None, "1")]
        app._queue_paused = False
        # simulate a steer left buffered on the harness when the turn ended
        app.harness._steer_buffer = [("stranded", None)]
        started = []
        app._start_turn = lambda text, attachments=None: started.append(text) or _noop()
        await app._after_turn()
        # "stranded" was prepended to the front and drained first;
        # "existing" remains at position 0 after the drain.
        assert started[0] == "stranded"  # stranded steer was first to run
        assert app._queue[0].text == "existing"  # existing item is now front


@pytest.mark.anyio
async def test_stranded_steer_kept_on_paused_finish(tmp_path):
    """On a paused (cancel/error) finish, a stranded steer must be kept at the
    front of the queue but NOT run — it waits for the user to resume."""
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue_paused = True
        # simulate a steer buffered in the harness when the turn ended
        app.harness._steer_buffer = [("stranded", None)]
        start_calls = []
        app._start_turn = lambda text, attachments=None: start_calls.append(text) or _noop()
        await app._after_turn()
        # steer was NOT started (queue is paused)
        assert start_calls == []
        # steer was kept at front of the queue, not dropped
        assert app._queue[0].text == "stranded"


def _recording_streaming_harness(tmp_path, calls):
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

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


@pytest.mark.anyio
async def test_ctrl_g_posts_steer_message(tmp_path):
    from textual.app import App, ComposeResult

    posted = []

    class _App(App):
        def compose(self) -> ComposeResult:
            yield PromptInput()

        def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
            posted.append(event.value)

    app = _App()
    async with app.run_test() as pilot:
        await pilot.pause()
        pi = app.query_one(PromptInput)
        pi.focus()
        pi.text = "steer via ctrl-g"
        await pilot.press("ctrl+g")
        await pilot.pause()
    assert posted == ["steer via ctrl-g"]
