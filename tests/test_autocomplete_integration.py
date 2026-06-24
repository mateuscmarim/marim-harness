# tests/test_autocomplete_integration.py
"""Integration tests for the slash-command autocomplete in the TUI.

Uses Textual's pilot to simulate real typing and verify the end-to-end flow
from keystroke -> autocomplete visible -> selection -> prompt replacement.
"""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import OptionList

from marim_harness.interfaces.tui.widgets import PromptInput
from marim_harness.interfaces.tui.widgets.autocomplete import CommandAutocomplete


class _AcIntegrationApp(App):
    """Minimal app with the autocomplete wired up (mirrors HarnessApp wiring)."""

    def __init__(self):
        super().__init__()
        self._autocomplete = None

    def compose(self) -> ComposeResult:
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput()

    def _show_autocomplete(self, query: str) -> None:
        if self._autocomplete is None:
            self._autocomplete = self.query_one("#cmd-autocomplete", CommandAutocomplete)
        self._autocomplete.filter(query)

    def _hide_autocomplete(self) -> None:
        if self._autocomplete is not None:
            self._autocomplete.visible = False

    def autocomplete_navigate(self, delta: int) -> bool:
        if self._autocomplete is None:
            return False
        return self._autocomplete.move_highlight(delta)

    def autocomplete_accept(self) -> bool:
        if self._autocomplete is None:
            return False
        return self._autocomplete.accept_highlighted()

    def on_prompt_input_slash_changed(self, event: PromptInput.SlashChanged) -> None:
        first_line = event.value.split("\n", 1)[0]
        self._show_autocomplete(first_line[1:])

    def on_prompt_input_slash_dismissed(self, _) -> None:
        self._hide_autocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        prompt = self.query_one(PromptInput)
        prompt.text = f"/{event.command_name} "
        prompt.move_cursor(prompt.document.end)
        self._hide_autocomplete()
        prompt.focus()


@pytest.mark.anyio
async def test_typing_slash_shows_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")  # types "/"
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True


@pytest.mark.anyio
async def test_typing_slash_he_filters_to_help():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        assert len(ac._options) == 1
        assert ac._options[0][0] == "help"


@pytest.mark.anyio
async def test_deleting_slash_dismisses_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        await pilot.press("backspace")
        await pilot.pause()
        assert ac.visible is False


@pytest.mark.anyio
async def test_normal_text_no_autocomplete():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("h", "e", "l", "p")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is False


@pytest.mark.anyio
async def test_select_replaces_prompt_text():
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        # Simulate pressing Enter on the highlighted option.
        option_list = ac.query_one("#cmd-options")
        option_list.post_message(
            OptionList.OptionSelected(option_list, option_list.get_option_at_index(0), 0)
        )
        await pilot.pause()
        assert pi.text == "/help "
        assert ac.visible is False


@pytest.mark.anyio
async def test_tab_completes_highlighted_command():
    """Typing ``/`` then Tab completes the highlighted command into the prompt —
    the keyboard path that mirrors clicking an option."""
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "h", "e")  # filters to /help
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert pi.text == "/help "
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is False


@pytest.mark.anyio
async def test_down_moves_highlight_then_tab_completes():
    """Down steps the highlight to the second match; Tab completes that one."""
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash", "s")  # several commands start with 's'
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert len(ac._options) >= 2
        second = ac._options[1][0]
        await pilot.press("down")
        await pilot.pause()
        await pilot.press("tab")
        await pilot.pause()
        assert pi.text == f"/{second} "


@pytest.mark.anyio
async def test_tab_with_no_menu_does_not_complete():
    """Tab while no slash menu is open is a normal keystroke, not a completion."""
    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("h", "i")
        await pilot.press("tab")
        await pilot.pause()
        # No command was completed; the prompt keeps its typed text.
        assert pi.text.startswith("hi")


@pytest.mark.anyio
async def test_bare_slash_shows_all_commands():
    from marim_harness.interfaces.tui.commands import COMMANDS

    app = _AcIntegrationApp()
    async with app.run_test() as pilot:
        pi = app.query_one(PromptInput)
        pi.focus()
        await pilot.pause()
        await pilot.press("slash")
        await pilot.pause()
        ac = app.query_one(CommandAutocomplete)
        assert ac.visible is True
        unique_names = {c.name for c in COMMANDS}
        assert len(ac._options) == len(unique_names)
