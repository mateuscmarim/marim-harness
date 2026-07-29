"""Reactive queue display — renders queued user messages. Replaces the
manual QueuePanel.show_queue() / _render_queue() pattern."""
from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static

from ..queue import QueuedMessage, render_queue


class QueueDisplay(Static):
    """A reactive display for queued messages.

    Setting ``items`` to a non-empty list shows the queue; setting it to
    an empty list hides it. ``paused`` adds a pause badge.
    """

    items: reactive[list[QueuedMessage]] = reactive(list, init=False)
    paused: reactive[bool] = reactive(False, init=False)

    DEFAULT_CSS = """
    QueueDisplay {
        max-height: 8;
        overflow-y: auto;
        background: $panel;
        padding: 0 1;
    }
    """

    def __init__(self) -> None:
        super().__init__()
        self.display = False  # hidden when empty

    def watch_items(self, value: list[QueuedMessage]) -> None:
        """Show/hide based on item count."""
        self.display = bool(value)
        self._repaint()

    def watch_paused(self) -> None:
        """Re-render to show/hide pause badge."""
        self._repaint()

    def _repaint(self) -> None:
        """Render the queue items as markup."""
        if not self.items:
            return
        header = "Queued — paused" if self.paused else "Queued"
        self.update(f"[bold]{header}[/]\n" + render_queue(self.items))
