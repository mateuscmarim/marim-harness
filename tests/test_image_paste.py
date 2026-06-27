# tests/test_image_paste.py
import pytest

from marim_harness.interfaces.tui.app import HarnessApp


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.runtime.deps import Deps
    from marim_harness.runtime.harness import Harness
    from marim_harness.runtime.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_ctrl_v_caches_image_and_inserts_token(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert "[Image #1]" in box.text
        assert len(box.attachments) == 1
        path, media_type = box.attachments[0]
        assert path.exists() and media_type == "image/png"


@pytest.mark.anyio
async def test_submit_forwards_attachments_to_run_turn(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    seen = {}

    async def fake_run_turn(prompt, event_stream_handler=None, attachments=None):
        seen["attachments"] = attachments
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] what is this?"
        await pilot.press("enter")
        await pilot.pause()
        assert seen["attachments"] == [(b"\x89PNGbytes", "image/png")]


@pytest.mark.anyio
async def test_paste_image_path_inserts_token_not_path(tmp_path, monkeypatch):
    """Bracketed file-path paste attaches the image and inserts [Image #N],
    suppressing TextArea's default raw-path insertion."""
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from textual import events

    from marim_harness.interfaces.tui.widgets import PromptInput

    img = tmp_path / "foo.png"
    img.write_bytes(b"\x89PNGbytes")
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        box.post_message(events.Paste(str(img)))
        await pilot.pause()
        assert "[Image #1]" in box.text
        assert str(img) not in box.text  # raw path must NOT appear
        assert len(box.attachments) == 1


@pytest.mark.anyio
async def test_ctrl_v_invokes_paste_image_hook(tmp_path):
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        calls = []
        box._on_paste_image = lambda: (calls.append(1), False)[1]
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert calls == [1]


@pytest.mark.anyio
async def test_text_only_model_blocks_image_submit_with_warning(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import NoticeMessage, PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    called = {"run": False}

    async def fake_run_turn(*a, **k):
        called["run"] = True
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        app.harness.model_id = "b/text"
        app._vision_caps = {"b/text": False}
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] look"
        await pilot.press("enter")
        await pilot.pause()
        assert called["run"] is False
        log = app.query_one("#log")
        assert any(isinstance(w, NoticeMessage) for w in log.walk_children())


@pytest.mark.anyio
async def test_startup_seeds_vision_caps(tmp_path):
    from marim_harness.workspace import ModelEntry

    class _FakeSource:
        is_local = False
        async def list_models(self):
            return [ModelEntry(id="x/text", name="X", supports_images=False)]

    app = _app(tmp_path)
    app.harness.model_source = _FakeSource()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.pause()
        assert app._vision_caps == {"x/text": False}


@pytest.mark.anyio
async def test_unknown_capability_allows_image_submit(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    called = {"run": False}

    async def fake_run_turn(*a, **k):
        called["run"] = True
        return "ok"

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.harness.run_turn = fake_run_turn
        app._vision_caps = {}  # unknown
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        box.text = "[Image #1] look"
        await pilot.press("enter")
        await pilot.pause()
        assert called["run"] is True


@pytest.mark.anyio
async def test_backspace_after_marker_removes_marker_and_attachment(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        assert box.text == "[Image #1]" and len(box.attachments) == 1
        box.move_cursor((0, len("[Image #1]")))  # cursor just after the ']'
        await pilot.press("backspace")
        await pilot.pause()
        assert box.text == ""
        assert box.attachments == []


@pytest.mark.anyio
async def test_delete_on_marker_start_removes_whole_marker(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (b"\x89PNGbytes", "image/png"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        await pilot.press("ctrl+v")
        await pilot.pause()
        box.move_cursor((0, 0))  # cursor on the '[' — delete should take the whole marker
        await pilot.press("delete")
        await pilot.pause()
        assert box.text == ""
        assert box.attachments == []


@pytest.mark.anyio
async def test_deleting_middle_marker_renumbers_remaining(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_IMAGE_CACHE_DIR", str(tmp_path / "cache"))
    from marim_harness import images
    from marim_harness.interfaces.tui.widgets import PromptInput

    seq = iter([b"img1", b"img2", b"img3"])
    monkeypatch.setattr(images, "read_clipboard_image",
                        lambda: (next(seq), "image/png"))
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        for _ in range(3):
            await pilot.press("ctrl+v")
            await pilot.pause()
        assert box.text == "[Image #1][Image #2][Image #3]"
        paths = [p for p, _ in box.attachments]
        box.move_cursor((0, 15))  # inside the [Image #2] span (offsets 10..20)
        await pilot.press("backspace")
        await pilot.pause()
        # the middle marker is gone and the third renumbers down to #2
        assert box.text == "[Image #1][Image #2]"
        assert [p for p, _ in box.attachments] == [paths[0], paths[2]]


@pytest.mark.anyio
async def test_backspace_in_plain_text_is_unaffected(tmp_path):
    from marim_harness.interfaces.tui.widgets import PromptInput

    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        box = app.query_one(PromptInput)
        box.focus()
        box.text = "hello"
        box.move_cursor((0, 5))
        await pilot.press("backspace")
        await pilot.pause()
        assert box.text == "hell"
        assert box.attachments == []
