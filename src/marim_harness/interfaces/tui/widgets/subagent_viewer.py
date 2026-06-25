"""The sub-agents screen's master list: one row per spawned sub-agent, with live
status/stats columns. Pure presentation — the app drives it via ``refresh_rows``
and reads the cursor via ``selected_index``; row selection (the DataTable cursor)
chooses which transcript the detail host shows."""

from textual.widgets import DataTable

from .subagent_stats import row_cells

_COLUMNS = ("", "agent", "tools", "tokens", "cost", "dur")


class SubAgentList(DataTable):
    """The left pane: a focusable row-cursor table of session sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-list", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        for c in _COLUMNS:
            self.add_column(c, key=c)

    def refresh_rows(self, subagents: list, selected: int) -> None:
        """Rebuild every row from ``subagents`` and place the cursor on
        ``selected``. N is the session's sub-agent count (small), so a full rebuild
        per change is cheap and avoids per-cell key bookkeeping."""
        self.clear()
        for w in subagents:
            self.add_row(*row_cells(w))
        if self.row_count:
            self.move_cursor(row=max(0, min(selected, self.row_count - 1)))

    def selected_index(self) -> int:
        return self.cursor_row
