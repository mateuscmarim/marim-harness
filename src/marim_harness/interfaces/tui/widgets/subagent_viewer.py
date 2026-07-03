"""The sub-agents screen's master list: one row per spawned sub-agent, with live
status/stats columns. Pure presentation — the app drives it via ``refresh_rows``
and reads the cursor via ``cursor_row``; row selection (the DataTable cursor)
chooses which transcript the detail host shows."""

from textual.widgets import DataTable

from .subagent_stats import _row_prefix, row_cells, tree_order

# (label, width) per column. Fixed widths keep the stat columns (tools/tokens/
# cost/dur) visible and aligned: DataTable truncates the long "{type} — title"
# cell to the agent column's width instead of letting it push the stats off the
# pane's right edge. The pane width in styles.tcss is sized to fit their sum.
_COLUMNS = (
    ("", 2),
    ("agent", 28),
    ("tools", 5),
    ("tokens", 6),
    ("cost", 6),
    ("dur", 6),
)


class SubAgentList(DataTable):
    """The left pane: a focusable row-cursor table of session sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-list", cursor_type="row", zebra_stripes=True)

    def on_mount(self) -> None:
        for label, width in _COLUMNS:
            self.add_column(label, key=label, width=width)

    def refresh_rows(self, subagents: list, selected: int | None = None) -> None:
        """Rebuild every row from ``subagents``. With ``selected`` given, place the
        cursor there (open/navigate); with ``selected`` None, preserve the *current*
        cursor — a live stats repaint must not move the user's selection. N is the
        session's sub-agent count (small), so a full rebuild per change is cheap and
        avoids per-cell key bookkeeping.

        The rebuild is wrapped in ``prevent(RowHighlighted)`` so its programmatic
        cursor moves (clear() resets the cursor to row 0; move_cursor restores it)
        don't emit highlight events. Otherwise the app's on_data_table_row_highlighted
        would mistake them for user navigation and, during a fan-out's per-frame
        repaints, fight the user's selection back to the first row."""
        keep = self.cursor_row if selected is None else selected
        with self.prevent(DataTable.RowHighlighted):
            self.clear()
            for tr in tree_order(subagents):
                self.add_row(*row_cells(tr.agent, _row_prefix(tr.depth, tr.is_last)))
            if self.row_count:
                self.move_cursor(row=max(0, min(keep, self.row_count - 1)))
