"""A modal for choosing the active model: a filter box over the provider's
catalog, with free-text entry when no catalog is available (e.g. local).

The catalog can be supplied up front (``entries``) or loaded lazily in the
modal's own worker (``fetch``). The lazy path is what keeps the picker snappy:
the modal opens immediately with free-text enabled and a "loading…" line, then
populates the list when the fetch returns — a slow or failing provider never
blocks the UI, and you can still type an id while it loads."""

from collections.abc import Awaitable, Callable

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...workspace import ModelEntry, filter_entries


class ModelPickerModal(ModalScreen[str | None]):
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
    #model-status {
        color: $text-muted;
    }
    #model-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(
        self,
        entries: list[ModelEntry] | None = None,
        allow_free_text: bool = False,
        current: str | None = None,
        fetch: Callable[[], Awaitable[list[ModelEntry]]] | None = None,
        is_local: bool = False,
    ) -> None:
        super().__init__()
        self.entries = entries or []
        self.is_local = is_local
        self._fetch = fetch
        # While a fetch is pending the catalog is unknown, so allow free-text up
        # front — the user can type an id without waiting. A synchronous build
        # (no fetch) honours the caller's allow_free_text verbatim.
        self._loading = fetch is not None
        self.allow_free_text = allow_free_text or self._loading or is_local
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
            yield Static("", id="model-status")
            yield OptionList(id="model-options")

    def on_mount(self) -> None:
        self._populate(self.entries)
        self._render_status()
        self.query_one("#model-filter", Input).focus()
        if self._fetch is not None:
            self.run_worker(self._load_catalog(), exclusive=True)

    async def _load_catalog(self) -> None:
        """Pull the catalog in the background and fold it in: repopulate the list
        (respecting any filter typed while loading) and recompute free-text from
        the result — a catalog narrows you to its entries; an empty/failed remote
        fetch keeps free-text open so an id can still be typed."""
        assert self._fetch is not None  # only scheduled when a fetch was provided
        entries = await self._fetch()
        self.entries = entries
        self._loading = False
        self.allow_free_text = self.is_local or not entries
        current_filter = self.query_one("#model-filter", Input).value
        self._populate(filter_entries(self.entries, current_filter))
        self._render_status()

    def _render_status(self) -> None:
        status = self.query_one("#model-status", Static)
        if self._loading:
            status.update("Loading catalog…")
        elif self._fetch is not None and not self.entries and not self.is_local:
            status.update("Couldn't fetch the catalog — type a model id.")
        else:
            status.update("")

    def _populate(self, entries: list[ModelEntry]) -> None:
        options = self.query_one("#model-options", OptionList)
        options.clear_options()
        for entry in entries:
            label = entry.id if entry.id == entry.name else f"{entry.id}  —  {entry.name}"
            if entry.provider:
                label = f"{label}  · {entry.provider}"
            options.add_option(Option(label, id=entry.qualified))
        if entries:
            options.highlighted = 0

    def _highlighted_id(self) -> str | None:
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
