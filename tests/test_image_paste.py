# tests/test_image_paste.py
import pytest

from marim_harness.interfaces.tui.app import HarnessApp


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
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
