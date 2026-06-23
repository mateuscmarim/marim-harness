from marim_harness.interfaces.tui.queue import QueuedMessage, render_queue


def test_queued_message_holds_text_attachments_id():
    m = QueuedMessage("hello", None, "1")
    assert m.text == "hello"
    assert m.attachments is None
    assert m.id == "1"


def test_render_queue_lists_items_in_order():
    items = [QueuedMessage("first", None, "1"), QueuedMessage("second", None, "2")]
    out = render_queue(items)
    assert "1. first" in out
    assert "2. second" in out
    # first appears before second
    assert out.index("first") < out.index("second")


def test_render_queue_shows_attachment_count():
    items = [QueuedMessage("with files", [(b"x", "image/png"), (b"y", "image/png")], "1")]
    assert "📎2" in render_queue(items)


def test_render_queue_escapes_markup_in_user_text():
    # A '[' in user text must not be parsed as Textual markup.
    items = [QueuedMessage("do [this]", None, "1")]
    out = render_queue(items)
    assert "\\[this]" in out  # escaped open bracket


from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets.prompt import PromptInput
from marim_harness.permissions import Mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_submit_while_busy_enqueues(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sentinel = object()
        app._turn_worker = sentinel  # simulate a running turn
        await app.on_prompt_input_submitted(PromptInput.Submitted("queued one", []))
        assert [m.text for m in app._queue] == ["queued one"]
        assert app._turn_worker is sentinel  # no new worker started


@pytest.mark.anyio
async def test_idle_submit_runs_immediately(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_worker = None
        await app.on_prompt_input_submitted(PromptInput.Submitted("hello", []))
        assert app._queue == []
        assert app._turn_worker is not None  # a worker was spawned


from asyncio import CancelledError

from marim_harness.interfaces.tui.queue import QueuedMessage


@pytest.mark.anyio
async def test_after_turn_drains_next_when_not_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app._start_turn = fake_start
        app._queue = [QueuedMessage("a", None, "1"), QueuedMessage("b", None, "2")]
        app._queue_paused = False
        await app._after_turn()
        assert started == ["a"]
        assert [m.text for m in app._queue] == ["b"]


@pytest.mark.anyio
async def test_after_turn_does_not_drain_when_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        woke = []
        app._maybe_wake = lambda: woke.append(True)
        app._queue = [QueuedMessage("a", None, "1")]
        app._queue_paused = True
        await app._after_turn()
        assert [m.text for m in app._queue] == ["a"]  # untouched
        assert woke == [True]  # fell through to wake


@pytest.mark.anyio
async def test_error_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise RuntimeError("boom")

        app.harness.run_turn = boom
        app._queue = [QueuedMessage("a", None, "1")]
        await app._run_turn("x")  # caught by the except Exception branch
        assert app._queue_paused is True
        assert [m.text for m in app._queue] == ["a"]


@pytest.mark.anyio
async def test_cancel_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise CancelledError()

        app.harness.run_turn = boom
        app._queue = [QueuedMessage("a", None, "1")]
        with pytest.raises(CancelledError):
            await app._run_turn("x")
        assert app._queue_paused is True


@pytest.mark.anyio
async def test_run_queued_action_resumes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app._start_turn = fake_start
        app._turn_worker = None
        app._queue_paused = True
        app._queue = [QueuedMessage("a", None, "1")]
        await app.action_run_queued()
        assert app._queue_paused is False
        assert started == ["a"]


def test_render_queue_embeds_click_actions():
    items = [QueuedMessage("draft one", None, "7")]
    out = render_queue(items)
    assert "@click=app.edit_queued('7')" in out
    assert "@click=app.remove_queued('7')" in out


@pytest.mark.anyio
async def test_remove_queued_drops_item(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("a", None, "1"), QueuedMessage("b", None, "2")]
        app.action_remove_queued("1")
        assert [m.id for m in app._queue] == ["2"]


@pytest.mark.anyio
async def test_edit_queued_pops_text_into_input(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("draft me", None, "1")]
        await app.action_edit_queued("1")
        assert app._queue == []  # removed from the queue
        assert app.query_one(PromptInput).text == "draft me"


@pytest.mark.anyio
async def test_edit_queued_unknown_id_is_noop(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("a", None, "1")]
        await app.action_edit_queued("nope")
        assert [m.id for m in app._queue] == ["1"]
