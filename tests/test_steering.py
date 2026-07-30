import asyncio
from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets.prompt import PromptInput
from marim_harness.runtime.permissions import Mode
from tests.conftest import _make_deps


@pytest.fixture
def anyio_backend():
    return "asyncio"


async def _noop():
    return None


def _harness(tmp_path: Path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    return Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(),
        _make_deps(tmp_path), instructions="test",
    )


class _FakeCtx:
    """Stands in for a live RunContext: enqueue appends one PendingMessage
    stand-in to the shared pending_messages queue, mirroring pydantic-ai's
    contract that _flush_steers builds its delivery receipts on."""

    def __init__(self):
        self.calls = []
        self.pending_messages = []

    def enqueue(self, *content, priority="asap"):
        self.calls.append((content, priority))
        self.pending_messages.append(object())


def test_reclaim_rebuffers_steer_still_in_queue(tmp_path):
    """A steer whose PendingMessage receipt is still sitting in the run's queue
    was never drained into a request — the round died first. It must be
    re-buffered, regardless of what its text looks like (the pre-2.x text
    heuristic could confuse a short steer like 'continue' with history)."""
    h = _harness(tmp_path)
    tc = h.turn_controller
    pm = object()
    queue = [pm]  # the run died with the steer still queued
    tc._inflight_steers = [("continue", None, pm, queue)]
    tc._reclaim_undelivered_steers()
    assert tc._steer_buffer == [("continue", None)]  # reclaimed, not dropped
    assert tc._inflight_steers == []


def test_reclaim_keeps_delivered_steer(tmp_path):
    """A drained steer's PendingMessage is removed from the queue in place by
    pydantic-ai's drain capability — the receipt object is gone, so the steer
    is delivered and must not be re-buffered, even if other messages remain."""
    h = _harness(tmp_path)
    tc = h.turn_controller
    pm = object()
    queue = [object()]  # something else queued; pm itself was drained
    tc._inflight_steers = [("continue", None, pm, queue)]
    tc._reclaim_undelivered_steers()
    assert tc._steer_buffer == []  # delivered → not reclaimed
    assert tc._inflight_steers == []


def test_reclaim_distinguishes_identical_steers_by_identity(tmp_path):
    """Two textually identical steers, one drained: object identity — not text —
    decides which one comes back. The pre-2.x text multiset approximated this;
    receipts make it exact."""
    h = _harness(tmp_path)
    tc = h.turn_controller
    pm1, pm2 = object(), object()
    queue = [pm2]  # pm1 drained, pm2 stranded
    tc._inflight_steers = [("yes", None, pm1, queue), ("yes", None, pm2, queue)]
    tc._reclaim_undelivered_steers()
    assert tc._steer_buffer == [("yes", None)]  # exactly one reclaimed
    assert tc._inflight_steers == []


def test_flush_records_one_receipt_per_steer(tmp_path):
    """_flush_steers captures each steer's receipt as the queue entry its own
    enqueue appended — the tail right after that enqueue, not a neighbour's."""
    h = _harness(tmp_path)
    tc = h.turn_controller
    ctx = _FakeCtx()
    tc._active_run_ctx = ctx
    tc._steer_buffer = [("first", None), ("second", None)]
    tc._flush_steers()
    assert tc._steer_buffer == []
    assert [t for t, _, _, _ in tc._inflight_steers] == ["first", "second"]
    receipts = [pm for _, _, pm, _ in tc._inflight_steers]
    assert receipts == ctx.pending_messages  # each steer got its own entry
    assert receipts[0] is not receipts[1]


def test_steer_enqueues_on_active_ctx(tmp_path):
    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h.turn_controller._active_run_ctx = ctx
    h.steer("go left")
    assert ctx.calls == [(("go left",), "asap")]
    assert h.turn_controller._steer_buffer == []  # flushed


def test_steer_with_attachments_enqueues_binary_content(tmp_path):
    from pydantic_ai import BinaryContent

    h = _harness(tmp_path)
    ctx = _FakeCtx()
    h.turn_controller._active_run_ctx = ctx
    h.steer("look", attachments=[(b"\x89PNG", "image/png")])
    content, priority = ctx.calls[0]
    assert content[0] == "look"
    assert isinstance(content[1], BinaryContent)
    assert content[1].media_type == "image/png"
    assert priority == "asap"


def test_steer_buffers_when_no_active_ctx(tmp_path):
    h = _harness(tmp_path)
    assert h.turn_controller._active_run_ctx is None
    h.steer("later")
    assert h.turn_controller._steer_buffer == [("later", None)]


def test_take_buffered_steers_returns_and_clears(tmp_path):
    h = _harness(tmp_path)
    h.steer("a")
    h.steer("b")
    assert h.take_buffered_steers() == [("a", None), ("b", None)]
    assert h.turn_controller._steer_buffer == []


@pytest.mark.anyio
async def test_steer_during_approval_gap_buffers_not_stale_ctx(tmp_path):
    """A steer that arrives while the approval modal is up (between rounds) must
    buffer for the next round, not enqueue onto the just-finished round's
    RunContext. Regression: the captured ctx was nulled only at turn end, so a
    steer in the inter-round gap hit a completed ctx."""
    import json

    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import DeltaToolCall, FunctionModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    (tmp_path / "a.txt").write_text("foo")

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    n = {"stream": 0}

    async def stream_fn(messages, info):
        n["stream"] += 1
        if n["stream"] == 1:
            # Round 1: a gated edit → deferred approval round.
            yield {
                0: DeltaToolCall(
                    name="edit_file",
                    json_args=json.dumps(
                        {"path": "a.txt",
                         "edits": [{"old_string": "foo", "new_string": "bar"}]}
                    ),
                    tool_call_id="tc-edit",
                )
            }
        else:
            yield "done"  # continuation finishes cleanly

    observed: dict = {}

    async def approve(_call):
        # Runs between rounds: round 1's run() has returned, so the captured ctx
        # must already be cleared. A steer here must buffer.
        observed["ctx"] = harness.turn_controller._active_run_ctx
        harness.steer("mid-approval steer")
        # Drain so the (empty) continuation doesn't enqueue onto a test ctx.
        observed["buffered"] = harness.take_buffered_steers()
        return True

    deps = _make_deps(tmp_path, mode=Mode.ask, request_approval=approve)
    harness = Harness(
        model=FunctionModel(fn, stream_function=stream_fn),
        provider=BuiltinToolProvider(),
        deps=deps,
        instructions="test",
    )

    async def handler(stream_ctx, events):
        async for _ in events:
            pass

    out = await harness.run_turn("change foo to bar", event_stream_handler=handler)

    assert out == "done"
    assert observed["ctx"] is None  # stale ctx cleared before the approval gap
    assert observed["buffered"] == [("mid-approval steer", None)]


@pytest.mark.anyio
async def test_steer_flushed_into_failing_round_is_reclaimed(tmp_path):
    """A steer flushed onto a live round is only *scheduled*: pydantic-ai
    delivers 'asap' content at the next request boundary. A round that dies
    before reaching one used to silently drop the steer — the buffer was
    already cleared and the run was gone. It must be re-buffered so
    take_buffered_steers hands it back to the queue."""
    from pydantic_ai.messages import ModelResponse, TextPart
    from pydantic_ai.models.function import FunctionModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])

    async def stream_fn(messages, info):
        yield "partial "
        raise RuntimeError("round boom")

    harness = Harness(
        FunctionModel(fn, stream_function=stream_fn), BuiltinToolProvider(),
        _make_deps(tmp_path), instructions="test",
    )

    steered = {"done": False}

    async def handler(stream_ctx, events):
        async for _ in events:
            if not steered["done"]:
                steered["done"] = True
                harness.steer("important correction")

    with pytest.raises(RuntimeError):
        await harness.run_turn("go", event_stream_handler=handler)

    assert steered["done"], "test never steered"
    # The steer never reached a request boundary; it must be reclaimed, not lost.
    assert harness.take_buffered_steers() == [("important correction", None)]


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

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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
        assert app.queue.items == []  # not queued
        assert app._turn_worker is not None  # no new worker


@pytest.mark.anyio
async def test_steer_while_idle_runs_normally(tmp_path):
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []
        app.start_turn = lambda text, attachments=None: started.append(text) or _noop()
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
        app.queue.enqueue("existing")
        app.queue.paused = False
        # simulate a steer left buffered on the harness when the turn ended
        app.harness.turn_controller._steer_buffer = [("stranded", None)]
        started = []
        app.start_turn = lambda text, attachments=None: started.append(text) or _noop()
        await app.queue.after_turn()
        # "stranded" was prepended to the front and drained first;
        # "existing" remains at position 0 after the drain.
        assert started[0] == "stranded"  # stranded steer was first to run
        assert app.queue.items[0].text == "existing"  # existing item is now front


@pytest.mark.anyio
async def test_stranded_steer_kept_on_paused_finish(tmp_path):
    """On a paused (cancel/error) finish, a stranded steer must be kept at the
    front of the queue but NOT run — it waits for the user to resume."""
    app = _tui_app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.paused = True
        # simulate a steer buffered in the harness when the turn ended
        app.harness.turn_controller._steer_buffer = [("stranded", None)]
        start_calls = []
        app.start_turn = lambda text, attachments=None: start_calls.append(text) or _noop()
        await app.queue.after_turn()
        # steer was NOT started (queue is paused)
        assert start_calls == []
        # steer was kept at front of the queue, not dropped
        assert app.queue.items[0].text == "stranded"


def _recording_streaming_harness(tmp_path, calls):
    from collections.abc import AsyncIterator

    from pydantic_ai.messages import ModelMessage
    from pydantic_ai.models.function import (
        AgentInfo,
        DeltaToolCall,
        FunctionModel,
    )

    from marim_harness.runtime.harness import Harness
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
        _make_deps(tmp_path), instructions="test",
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
            if h.turn_controller._active_run_ctx is not None:
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
