"""Tests for the /think modal (``ThinkingPickerModal``).

It's a small screen, but it was the one TUI module with no coverage to speak of
— and every branch in it is a behaviour the user feels: which level Enter
re-picks by default, what a selection dismisses with, and what Escape returns.
"""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from marim_harness.interfaces.tui.thinking_picker import ThinkingPickerModal
from marim_harness.thinking import THINKING_LEVELS


class _Host(App):
    """Bare app that pushes the modal and records what it dismissed with."""

    def __init__(self, current: str | None) -> None:
        super().__init__()
        self.current = current
        self.picked: str | None | object = _UNSET

    def compose(self) -> ComposeResult:
        return iter(())

    async def open_picker(self) -> None:
        def _done(result: str | None) -> None:
            self.picked = result

        await self.push_screen(ThinkingPickerModal(self.current), _done)


_UNSET = object()


def _options(app: _Host) -> OptionList:
    return app.screen.query_one("#thinking-options", OptionList)


@pytest.mark.anyio
async def test_lists_every_thinking_level_in_vocabulary_order():
    """The list is the thinking vocabulary verbatim — a fixed six-item list, not
    a filtered/dynamic catalog like the model picker's."""
    app = _Host(None)
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        options = _options(app)
        assert options.option_count == len(THINKING_LEVELS)
        ids = [options.get_option_at_index(i).id for i in range(options.option_count)]
        assert ids == list(THINKING_LEVELS)


@pytest.mark.anyio
async def test_highlights_the_current_level_so_enter_re_picks_it():
    app = _Host("high")
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        assert _options(app).highlighted == THINKING_LEVELS.index("high")


@pytest.mark.anyio
@pytest.mark.parametrize("current", [None, "not-a-level"])
async def test_unknown_or_absent_current_falls_back_to_the_first_level(current):
    """A level the vocabulary doesn't contain (a stale persisted value, say) must
    not leave the list unhighlighted — Enter would then dismiss with nothing."""
    app = _Host(current)
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        assert _options(app).highlighted == 0


@pytest.mark.anyio
async def test_title_shows_the_current_level():
    app = _Host("minimal")
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        title = str(app.screen.query_one("#thinking-title").render())
        assert "current: minimal" in title


@pytest.mark.anyio
async def test_title_omits_the_current_level_when_there_is_none():
    app = _Host(None)
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        title = str(app.screen.query_one("#thinking-title").render())
        assert "current" not in title


@pytest.mark.anyio
async def test_selecting_dismisses_with_the_chosen_level():
    """Dismisses with the option *id* (the level slug), not its display label —
    the caller feeds it straight to parse_thinking_level."""
    app = _Host("off")
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        _options(app).highlighted = THINKING_LEVELS.index("medium")
        await pilot.press("enter")
        await pilot.pause()
        assert app.picked == "medium"


@pytest.mark.anyio
async def test_escape_cancels_with_none():
    """Escape must dismiss with None, not with the highlighted level — otherwise
    opening the picker to look would silently change the thinking level."""
    app = _Host("high")
    async with app.run_test() as pilot:
        await app.open_picker()
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
        assert app.picked is None
