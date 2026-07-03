"""The full-bleed sub-agents screen: a session summary bar, the agent list, and
the transcript detail host. (The container ``SubAgentsView`` is added in a later
step; this module starts with the summary bar so it can be tested on its own.)"""

from __future__ import annotations

import contextlib

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Static

from .subagent_detail import SubAgentDetailHost
from .subagent_stats import SummaryStats, aggregate
from .subagent_viewer import SubAgentList

_HINTS = "Esc back · ↑↓ select · Tab switch pane · t task · r resume"


class SubAgentSummary(Static):
    """The top roll-up bar: total agents (running/done/failed) + summed tokens and
    cost across the session's sub-agents."""

    def __init__(self) -> None:
        super().__init__(id="subagent-summary")

    def refresh_totals(self, stats: SummaryStats) -> None:
        mid = f"{stats.running} running · {stats.done} done · {stats.failed} failed"
        if stats.waiting:
            mid = f"{stats.waiting} waiting · {mid}"
        left = f"{stats.total} sub-agents · {mid}"
        right = f"{stats.tokens:,} tokens"
        if stats.cost_text:
            right = f"{right} · {stats.cost_text}"
        self.update(Content(f"{left}    {right}"))


class SubAgentsView(Vertical):
    """The full-bleed sub-agents screen. Hidden until ``ctrl+x``; when shown it
    covers the main log (the app toggles ``display`` and focus). Owns the in-view
    bindings and a ``repaint`` that repaints the summary + list from the renderer's
    ``subagents`` list."""

    BINDINGS = [
        Binding("escape", "app.close_subagents", "Back", show=False),
        Binding("ctrl+x", "app.close_subagents", "Close", show=False),
        Binding("tab", "focus_next_pane", "Switch pane", show=False),
        Binding("shift+tab", "focus_next_pane", "Switch pane", show=False),
        Binding("t", "toggle_task", "Task", show=False),
        Binding("r", "resume_agent", "Resume", show=False),
    ]

    def __init__(self) -> None:
        super().__init__(id="subagents-view")
        self.display = False

    def compose(self) -> ComposeResult:
        yield SubAgentSummary()
        with Horizontal(id="subagents-body"):
            yield SubAgentList()
            yield SubAgentDetailHost(id="subagent-detail-host")
        yield Static(_HINTS, id="subagent-hints")

    @property
    def list(self) -> SubAgentList:
        return self.query_one(SubAgentList)

    @property
    def host(self) -> SubAgentDetailHost:
        return self.query_one(SubAgentDetailHost)

    def repaint(self, subagents: list, cost_of, selected: int | None = None) -> None:
        """Repaint the summary + list. ``selected`` None preserves the list cursor
        (a live stats repaint); an int forces it (open/navigate).

        Live stream flush ticks drive this via ``drain_subagents_repaint``, so a
        tick can land between this view being created and its ``compose`` children
        mounting — the summary/list aren't queryable yet. Skip that tick (the next
        one repaints once they exist) rather than crash the live render path."""
        try:
            summary = self.query_one(SubAgentSummary)
            rows = self.list
        except NoMatches:
            return
        summary.refresh_totals(aggregate(subagents, cost_of))
        rows.refresh_rows(subagents, selected)

    def action_focus_next_pane(self) -> None:
        """Toggle focus between the list and the visible transcript pane."""
        if self.list.has_focus:
            with contextlib.suppress(Exception):
                self.host.query_one(f"#{self.host.current}").focus()
        else:
            self.list.focus()

    def action_resume_agent(self) -> None:
        """Resume the selected interrupted sub-agent (the 'r' key)."""
        self.app.subagents.resume_selected()  # type: ignore[attr-defined]

    def action_toggle_task(self) -> None:
        """Expand/collapse the full-task disclosure on the visible pane (the 't'
        key). A no-op when no pane is current or it has no task."""
        sid = self.host.current_sid()
        if sid is None:
            return
        pane = self.host.pane(sid)
        if pane is not None:
            pane.toggle_task()
