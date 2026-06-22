"""Tests for the slash-command autocomplete dropdown."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from marim_harness.interfaces.tui.widgets.autocomplete import CommandAutocomplete
from marim_harness.interfaces.tui.widgets import PromptInput


class _AcApp(App):
    """Minimal host for testing CommandAutocomplete in isolation."""

    def __init__(self):
        super().__init__()
        self.selected: list[str] = []

    def compose(self) -> ComposeResult:
        yield PromptInput()
        yield CommandAutocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        self.selected.append(event.command_name)


@pytest.mark.anyio
async def test_filter_empty_query_shows_all_commands():
    """An empty query (bare ``/``) should list every command."""
    from marim_harness.interfaces.tui.commands import COMMANDS

    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("")
        await pilot.pause()
        assert ac.visible is True
        options = ac.query_one("#cmd-options")
        # At least as many options as there are unique command names.
        assert options.option_count >= len({c.name for c in COMMANDS})


@pytest.mark.anyio
async def test_filter_prefix_matches_name():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("he")
        await pilot.pause()
        assert ac.visible is True
        options = ac.query_one("#cmd-options")
        assert options.option_count == 1  # only "help"


@pytest.mark.anyio
async def test_filter_prefix_matches_alias():
    """``?`` is an alias for help; ``ls`` for sessions."""
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("?")
        await pilot.pause()
        assert ac.visible is True
        # The canonical name is used, not the alias.
        assert len(ac._options) == 1
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_filter_is_case_insensitive():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("HELP")
        await pilot.pause()
        assert ac.visible is True
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_filter_no_match_hides_widget():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("xyz_nonexistent")
        await pilot.pause()
        assert ac.visible is False


@pytest.mark.anyio
async def test_filter_prefix_model():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("model")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "model"


@pytest.mark.anyio
async def test_filter_alias_ls():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("ls")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "sessions"


@pytest.mark.anyio
async def test_filter_alias_cost():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("cos")
        await pilot.pause()
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "usage"


@pytest.mark.anyio
async def test_dismiss_hides_and_posts_message():
    dismissed = []

    class DApp(App):
        def compose(self) -> ComposeResult:
            yield CommandAutocomplete()

        def on_command_autocomplete_dismissed(self, _):
            dismissed.append(True)

    app = DApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("help")
        await pilot.pause()
        assert ac.visible is True
        ac.dismiss()
        await pilot.pause()
        assert ac.visible is False
        assert dismissed == [True]


@pytest.mark.anyio
async def test_select_posts_command_selected():
    app = _AcApp()
    async with app.run_test() as pilot:
        ac = app.query_one(CommandAutocomplete)
        ac.filter("help")
        await pilot.pause()
        # Simulate selecting the first option by posting the message directly.
        option_list = ac.query_one("#cmd-options")
        option_list.post_message(
            OptionList.OptionSelected(option_list, option_list.get_option_at_index(0), 0)
        )
        await pilot.pause()
        assert app.selected == ["help"]
