"""Slash-command autocomplete dropdown for the TUI prompt.

Displays a filtered list of commands above the prompt when the user types
``/``.  Uses Textual's ``OptionList`` for keyboard/mouse navigation.
"""

from __future__ import annotations

from textual import on
from textual.message import Message
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option


class CommandAutocomplete(Static):
    """A floating dropdown that shows matching slash commands."""

    class CommandSelected(Message):
        """Posted when the user picks a command from the list."""

        def __init__(self, command_name: str) -> None:
            self.command_name = command_name
            super().__init__()

    class Dismissed(Message):
        """Posted when the widget is dismissed (Escape or empty results)."""

        def __init__(self) -> None:
            super().__init__()

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._options: list[tuple[str, str, str]] = []  # (name, display, canonical)
        self.can_focus = False

    def compose(self):
        yield OptionList(id="cmd-options")

    def on_mount(self) -> None:
        self.visible = False

    def filter(self, query: str) -> None:
        """Update the dropdown to show commands matching *query*.

        ``query`` is the text after the leading ``/``.  An empty query shows
        all commands.  Matching is a case-insensitive prefix check on the
        command name and all its aliases.
        """
        from ..commands import COMMANDS

        query_lower = query.lower()
        self._options = []
        seen: set[str] = set()
        for cmd in COMMANDS:
            names = [cmd.name, *cmd.aliases]
            if not any(n.lower().startswith(query_lower) for n in names):
                continue
            if cmd.name in seen:
                continue
            seen.add(cmd.name)
            display = f"/{cmd.name}  — {cmd.summary}"
            self._options.append((cmd.name, display, cmd.name))

        option_list = self.query_one("#cmd-options", OptionList)
        option_list.clear_options()
        if not self._options:
            self.visible = False
            return
        for _, display, _ in self._options:
            option_list.add_option(Option(display))
        self.visible = True
        # Highlight the first item.
        option_list.highlighted = 0

    @on(OptionList.OptionSelected)
    def _on_option_selected(self, event: OptionList.OptionSelected) -> None:
        idx = event.option_index
        if 0 <= idx < len(self._options):
            name = self._options[idx][0]
            self.visible = False
            self.post_message(self.CommandSelected(name))

    def dismiss(self) -> None:
        """Hide the dropdown and post Dismissed."""
        self.visible = False
        self.post_message(self.Dismissed())

    def _on_key(self, event) -> None:
        if event.key == "escape":
            event.prevent_default()
            event.stop()
            self.dismiss()
