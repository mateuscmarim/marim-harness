"""A modal for choosing the active model: a filter box over the provider's
catalog, with free-text entry when no catalog is available (e.g. local)."""

from typing import Optional

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ..catalog import ModelEntry, filter_entries


class ModelPickerModal(ModalScreen[Optional[str]]):
    """Dismisses with the chosen model id, or None if cancelled."""

    CSS = """
    ModelPickerModal {
        align: center middle;
    }
    #model-box {
        width: 80%;
        max-width: 100;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #model-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #model-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, entries: list[ModelEntry], allow_free_text: bool = False,
                 current: Optional[str] = None) -> None:
        super().__init__()
        self.entries = entries
        self.allow_free_text = allow_free_text
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="model-box"):
            title = "Select a model"
            if self.current:
                title += f"  (current: {self.current})"
            yield Static(title, id="model-title")
            placeholder = (
                "type a model id" if self.allow_free_text and not self.entries
                else "filter… (Tab to navigate, Enter to pick)"
            )
            yield Input(placeholder=placeholder, id="model-filter")
            yield OptionList(id="model-options")

    def on_mount(self) -> None:
        self._populate(self.entries)
        self.query_one("#model-filter", Input).focus()

    def _populate(self, entries: list[ModelEntry]) -> None:
        options = self.query_one("#model-options", OptionList)
        options.clear_options()
        for entry in entries:
            label = entry.id if entry.id == entry.name else f"{entry.id}  —  {entry.name}"
            options.add_option(Option(label, id=entry.id))
        if entries:
            options.highlighted = 0

    def _highlighted_id(self) -> Optional[str]:
        options = self.query_one("#model-options", OptionList)
        if options.option_count and options.highlighted is not None:
            return options.get_option_at_index(options.highlighted).id
        return None

    def on_input_changed(self, event: Input.Changed) -> None:
        self._populate(filter_entries(self.entries, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._highlighted_id()
        if choice is not None:
            self.dismiss(choice)
        elif self.allow_free_text and event.value.strip():
            self.dismiss(event.value.strip())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
