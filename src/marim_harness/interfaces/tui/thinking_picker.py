"""A modal for choosing the thinking level: a fixed six-item list (the thinking
vocabulary), unlike the model picker's dynamic catalog. Dismisses with the
chosen level, or None if cancelled."""

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import OptionList, Static
from textual.widgets.option_list import Option

from ...thinking import THINKING_LEVELS


class ThinkingPickerModal(ModalScreen[str | None]):
    """Dismisses with the chosen thinking level, or None if cancelled."""

    CSS = """
    ThinkingPickerModal {
        align: center middle;
    }
    #thinking-box {
        width: 60%;
        max-width: 60;
        height: auto;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #thinking-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #thinking-options {
        height: auto;
        max-height: 12;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, current: str | None = None) -> None:
        super().__init__()
        self.current = current

    def compose(self) -> ComposeResult:
        with Vertical(id="thinking-box"):
            title = "Select thinking level"
            if self.current:
                title += f"  (current: {self.current})"
            yield Static(title, id="thinking-title")
            options = OptionList(id="thinking-options")
            for level in THINKING_LEVELS:
                options.add_option(Option(level, id=level))
            yield options

    def on_mount(self) -> None:
        options = self.query_one("#thinking-options", OptionList)
        # Highlight the current level so Enter re-picks it by default.
        if self.current in THINKING_LEVELS:
            options.highlighted = THINKING_LEVELS.index(self.current)
        else:
            options.highlighted = 0
        options.focus()

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def action_cancel(self) -> None:
        self.dismiss(None)
