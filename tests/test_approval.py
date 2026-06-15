import pytest
from textual.app import App

from marim_harness.tui.approval import ApprovalModal, format_detail


class _Harness(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await self.push_screen_wait(
            ApprovalModal("edit_file", {"path": "a.txt"})
        )


@pytest.mark.anyio
async def test_approve_returns_true():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")  # approve binding
        await pilot.pause()
    assert app.result is True


@pytest.mark.anyio
async def test_deny_returns_false():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("d")  # deny binding
        await pilot.pause()
    assert app.result is False


def test_format_detail_edit_shows_diff():
    detail = format_detail(
        "edit_file",
        {"path": "a.txt", "old_string": "foo", "new_string": "bar"},
    )
    assert "a.txt" in detail
    assert "- foo" in detail
    assert "+ bar" in detail


def test_format_detail_bash_shows_command():
    detail = format_detail("run_command", {"command": "ls -la"})
    assert "$ ls -la" in detail


def test_format_detail_fallback_formats_args():
    detail = format_detail("some_tool", {"a": 1, "b": "two"})
    assert "a: 1" in detail
    assert "b: 'two'" in detail
    # not a raw dict dump
    assert "{'a'" not in detail
