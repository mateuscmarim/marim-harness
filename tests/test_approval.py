import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.interfaces.tui.interactions.approval import (
    ADDED_STYLE,
    REMOVED_STYLE,
    ApprovalPanel,
    format_detail,
)
from marim_harness.interfaces.tui.interactions.base import run_panel


def _styled_text(detail, needle: str) -> set[str]:
    """The set of span styles covering the first occurrence of ``needle``."""
    plain = detail.plain
    start = plain.index(needle)
    end = start + len(needle)
    return {
        str(span.style)
        for span in detail.spans
        if span.start <= start and span.end >= end
    }


class _Harness(App):
    def __init__(self):
        super().__init__()
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(
            self, ApprovalPanel("edit_file", {"path": "a.txt"})
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


@pytest.mark.anyio
async def test_escape_denies():
    """Esc backs out of the approval as a deny (consistent with the ask-user panel)."""
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is False


@pytest.mark.anyio
async def test_panel_removed_after_decision():
    app = _Harness()
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("a")
        await pilot.pause()
        assert not app.query(ApprovalPanel)


@pytest.mark.anyio
async def test_transcript_scrolls_while_approval_pending():
    app = _Harness()
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        assert app.result == "unset"


def test_format_detail_edit_shows_diff():
    detail = format_detail(
        "edit_file",
        {"path": "a.txt", "edits": [{"old_string": "foo", "new_string": "bar"}]},
    )
    assert "a.txt" in detail.plain
    assert "- foo" in detail.plain
    assert "+ bar" in detail.plain
    assert "edit 1" not in detail.plain  # single edit isn't numbered


def test_format_detail_edit_highlights_removed_and_added():
    detail = format_detail(
        "edit_file",
        {"path": "a.txt", "edits": [{"old_string": "foo", "new_string": "bar"}]},
    )
    assert REMOVED_STYLE in _styled_text(detail, "- foo")
    assert ADDED_STYLE in _styled_text(detail, "+ bar")


def test_format_detail_edit_numbers_multiple_edits():
    detail = format_detail(
        "edit_file",
        {
            "path": "a.txt",
            "edits": [
                {"old_string": "foo", "new_string": "bar"},
                {"old_string": "baz", "new_string": "qux"},
            ],
        },
    )
    assert "edit 1" in detail.plain
    assert "edit 2" in detail.plain
    assert "- foo" in detail.plain
    assert "+ qux" in detail.plain
    assert REMOVED_STYLE in _styled_text(detail, "- baz")
    assert ADDED_STYLE in _styled_text(detail, "+ qux")


def test_format_detail_bash_shows_command():
    detail = format_detail("run_command", {"command": "ls -la"})
    assert "$ ls -la" in detail.plain


def test_format_detail_write_file_highlights_content_as_added():
    detail = format_detail(
        "write_file", {"path": "new.py", "content": "print('hi')"}
    )
    assert "new.py" in detail.plain
    assert "print('hi')" in detail.plain
    assert ADDED_STYLE in _styled_text(detail, "print('hi')")


def test_format_detail_fallback_formats_args():
    detail = format_detail("some_tool", {"a": 1, "b": "two"})
    assert "a: 1" in detail.plain
    assert "b: 'two'" in detail.plain
    # not a raw dict dump
    assert "{'a'" not in detail.plain
