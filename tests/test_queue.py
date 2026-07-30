from asyncio import CancelledError
from pathlib import Path

import pytest

from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.queue import QueuedMessage, render_queue
from marim_harness.interfaces.tui.widgets.prompt import PromptInput
from tests.conftest import _make_deps


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


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.harness import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = _make_deps(tmp_path)
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
        assert [m.text for m in app.queue.items] == ["queued one"]
        assert app._turn_worker is sentinel  # no new worker started


@pytest.mark.anyio
async def test_idle_submit_runs_immediately(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_worker = None
        await app.on_prompt_input_submitted(PromptInput.Submitted("hello", []))
        assert app.queue.items == []
        worker = app._turn_worker
        assert worker is not None  # a worker was spawned
        # Drain the turn while #log is still mounted: the turn yields early now (its
        # rewind snapshot is offloaded to a thread), so an un-awaited worker would
        # resume mid-stream after run_test teardown removed #log and fail querying it.
        await worker.wait()


@pytest.mark.anyio
async def test_after_turn_drains_next_when_not_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app.start_turn = fake_start
        app.queue.enqueue("a")
        app.queue.enqueue("b")
        app.queue.paused = False
        await app.queue.after_turn()
        assert started == ["a"]
        assert [m.text for m in app.queue.items] == ["b"]


@pytest.mark.anyio
async def test_after_turn_does_not_drain_when_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        woke = []
        app.activity.maybe_wake = lambda: woke.append(True)
        app.queue.enqueue("a")
        app.queue.paused = True
        await app.queue.after_turn()
        assert [m.text for m in app.queue.items] == ["a"]  # untouched
        assert woke == [True]  # fell through to wake


@pytest.mark.anyio
async def test_error_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise RuntimeError("boom")

        app.harness.run_turn = boom
        app.queue.enqueue("a")
        await app._run_turn("x")  # caught by the except Exception branch
        assert app.queue.paused is True
        assert [m.text for m in app.queue.items] == ["a"]


@pytest.mark.anyio
async def test_cancel_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise CancelledError()

        app.harness.run_turn = boom
        app.queue.enqueue("a")
        with pytest.raises(CancelledError):
            await app._run_turn("x")
        assert app.queue.paused is True


@pytest.mark.anyio
async def test_run_queued_action_resumes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app.start_turn = fake_start
        app._turn_worker = None
        app.queue.paused = True
        app.queue.enqueue("a")
        await app.action_run_queued()
        assert app.queue.paused is False
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
        app.queue.enqueue("a")
        app.queue.enqueue("b")
        app.action_remove_queued("1")
        assert [m.id for m in app.queue.items] == ["2"]


@pytest.mark.anyio
async def test_edit_queued_pops_text_into_input(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("draft me")
        await app.action_edit_queued("1")
        assert app.queue.items == []  # removed from the queue
        assert app.query_one(PromptInput).text == "draft me"


@pytest.mark.anyio
async def test_edit_queued_restores_attachments(tmp_path):
    """Editing round-trips a queued item's image attachments: the text's
    ``[Image #N]`` markers ride along and the bytes are re-cached back onto the
    prompt, so resubmitting keeps the images."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("look [Image #1]", [(b"pngbytes", "image/png")])
        await app.action_edit_queued("1")
        await pilot.pause()
        prompt = app.query_one(PromptInput)
        assert prompt.text == "look [Image #1]"
        assert len(prompt.attachments) == 1
        path, media_type = prompt.attachments[0]
        assert media_type == "image/png"
        assert path.read_bytes() == b"pngbytes"  # re-cached on disk


@pytest.mark.anyio
async def test_edit_queued_without_attachments_clears_prior(tmp_path):
    """Editing a text-only item leaves no stale attachments on the prompt."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("draft")
        await app.action_edit_queued("1")
        await pilot.pause()
        assert app.query_one(PromptInput).attachments == []


@pytest.mark.anyio
async def test_edit_queued_unknown_id_is_noop(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("a")
        await app.action_edit_queued("nope")
        assert [m.id for m in app.queue.items] == ["1"]


@pytest.mark.anyio
async def test_remove_queued_action_string_routes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("a")
        app.queue.enqueue("b")
        # Equivalent to clicking the [@click=app.remove_queued('1')] link.
        await app.run_action("remove_queued('1')")
        await pilot.pause()
        assert [m.id for m in app.queue.items] == ["2"]


@pytest.mark.anyio
async def test_edit_queued_action_string_routes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("draft me")
        # Equivalent to clicking the [@click=app.edit_queued('1')] link.
        await app.run_action("edit_queued('1')")
        await pilot.pause()
        assert app.queue.items == []
        assert app.query_one(PromptInput).text == "draft me"


# --- Confirm-to-quit guard (Task 3) ---


@pytest.mark.anyio
async def test_quit_with_queued_warns_once_then_allows(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.queue.enqueue("a")
        assert app._maybe_warn_pending_quit() is True   # first: warned, cancel quit
        assert app._quit_warned_at is not None
        assert app._maybe_warn_pending_quit() is False  # second: proceed


@pytest.mark.anyio
async def test_quit_with_empty_queue_still_warns_once(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app._maybe_warn_pending_quit() is True   # first: warned, cancel quit
        assert app._maybe_warn_pending_quit() is False  # second: proceed


@pytest.mark.anyio
async def test_ctrl_c_warns_and_does_not_exit(tmp_path):
    """Pressing ctrl+c should always arm the guard and keep the app alive on
    the first attempt (i.e. _quit_warned_at gets set and the app is still
    running after the keypress), whether or not anything is queued."""
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("ctrl+c")
        await pilot.pause()
        assert app._quit_warned_at is not None
        assert app.is_running
