"""The full-screen sub-agent viewer chrome: a master list of all spawned
sub-agents (the left side panel) and a status footer. The transcript itself is not
owned here — it lives in each ``SubAgentWidget.body`` and is revealed in place by
the ``viewing`` class (see the app's ``action_toggle_subagents``). These two
widgets are pure presentation driven by the app, plus the keyboard navigation that
drives the viewer."""

from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import Static


class SubAgentList(VerticalScroll):
    """The viewer's left side panel: one row per session sub-agent, the current one
    highlighted. Focusable so its bindings own the arrow keys while the viewer is
    open (left/right switch, up/esc return to the log)."""

    can_focus = True

    BINDINGS = [
        Binding("left", "app.subagent_prev", "Prev", show=False),
        Binding("right", "app.subagent_next", "Next", show=False),
        Binding("up", "app.close_subagents", "Parent", show=False),
        Binding("escape", "app.close_subagents", "Parent", show=False),
        Binding("ctrl+x", "app.close_subagents", "Close", show=False),
    ]

    def __init__(self) -> None:
        self._inner = Static(id="subagent-list-inner")
        super().__init__(self._inner, id="subagent-list")

    def show_subagents(self, subagents: list, index: int) -> None:
        """Repaint the list, marking row ``index`` as the current selection."""
        lines = []
        for i, w in enumerate(subagents):
            glyph = {"done": "✓", "denied": "✕"}.get(w.status, "▸")
            text = f"{glyph} {w.agent_type} — {w.display_title()}"
            # Reverse-video the selected row; (text, style) assembly applies the
            # style without parsing the untrusted task text as markup.
            row = Content.assemble((text, "reverse")) if i == index else Content(text)
            lines.append(row)
        sep = Content("\n")
        body = sep.join(lines) if lines else Content("(no sub-agents)")
        self._inner.update(body)


class SubAgentFooter(Static):
    """The viewer's status footer: ``{type} ({i} of {N}) {spend}`` on the left and
    the dim navigation hints on the right."""

    _HINTS = "Parent up · Prev left · Next right"

    def show_status(self, agent_type: str, index: int, total: int, spend: str) -> None:
        left = f"{agent_type} ({index + 1} of {total})"
        if spend:
            left = f"{left} {spend}"
        # Pad the hints to the right edge; the Static spans the docked footer width,
        # so a wide gap reads as left/right justification without a table.
        self.update(Content.from_markup(f"{left}  [dim]·[/]  [dim]{self._HINTS}[/]"))
