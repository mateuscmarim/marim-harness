"""Self-contained compact notice — replaces the fragile 3-method lifecycle
(_on_compact_start → _on_compact → clear_compacting_notice) with reactive
state. Setting ``compacting = False`` always hides the notice, so there are
no dangling refs or manual cleanup paths."""
from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class CompactNotice(Static):
    """A reactive notice for the compaction pipeline.

    Three independent reactives drive three visual states:
    - ``compacting``: True → spinner + "Compacting…"
    - ``done``: True briefly → green checkmark, auto-hides after 2s
    - ``error_msg``: non-empty → red error, auto-hides after 5s

    The watcher on ``compacting`` hides the widget whenever it's set to
    False, clearing any other state. This eliminates the manual
    clear_compacting_notice try/except dance.
    """

    compacting: reactive[bool] = reactive(False, init=False, always_update=True)
    done: reactive[bool] = reactive(False, init=False)
    error_msg: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__()
        self.display = False  # hidden by default

    def watch_compacting(self, value: bool) -> None:
        """Show/hide the compaction spinner. Setting False always hides."""
        if value:
            self.display = True
            self.update("⟳ Compacting conversation…")
        else:
            self.display = False
            self.done = False
            self.error_msg = ""

    def watch_done(self, value: bool) -> None:
        """Show a green checkmark, then auto-hide after 2s."""
        if value:
            self.display = True
            self.update("✓ Compaction complete")
            self.set_timer(2.0, self._hide)

    def watch_error_msg(self, value: str) -> None:
        """Show a red error message, then auto-hide after 5s."""
        if value:
            self.display = True
            self.update(f"✗ {value}")
            self.set_timer(5.0, self._hide)

    def _hide(self) -> None:
        """Hide the notice — safe to call even if already hidden."""
        self.display = False
