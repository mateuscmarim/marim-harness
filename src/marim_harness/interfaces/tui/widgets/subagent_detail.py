"""The persistent transcript home for the sub-agents screen.

Each spawned sub-agent gets one ``SubAgentPane`` — the scroll container its
streamed transcript mounts into — and all panes live in a single
``SubAgentDetailHost`` (a ``ContentSwitcher``) that shows exactly one at a time.
Because the host is mounted for the session's life (hidden until the screen is
open), the live stream keeps mounting into a pane whether or not it's on screen,
so opening the screen mid-run shows an already-current transcript. Nothing is
ever reparented."""

import re

from textual.containers import VerticalScroll
from textual.content import Content
from textual.widgets import ContentSwitcher, Static

_DETACHED_NOTE = "detached — ran in background, no live transcript"


def pane_id(stream_id: str) -> str:
    """A valid Textual widget id for a pane keyed by a spawn's tool_call_id.
    Textual ids must match ``[a-zA-Z_-][a-zA-Z0-9_-]*``; tool_call_ids can carry
    other characters, so replace them and prefix to guarantee a letter start."""
    return "sap-" + re.sub(r"[^a-zA-Z0-9_-]", "-", stream_id or "none")


class SubAgentPane(VerticalScroll):
    """One sub-agent's transcript: a muted ``◼ {type} · {model}`` header, an
    (initially hidden) usage line mirroring the status bar's split + cost, and the
    streamed transcript widgets mounted after them. Replaces the old
    ``SubAgentWidget.body``."""

    def __init__(self, stream_id: str, agent_type: str, model_label: str) -> None:
        self.stream_id = stream_id
        label = f"{agent_type} · {model_label}" if model_label else agent_type
        self._header = Static(Content(f"◼ {label}"), classes="subagent-bhead")
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self._placeholder = Static(Content(_DETACHED_NOTE), classes="subagent-detached")
        self._placeholder.display = False
        super().__init__(
            self._header, self._usage_line, self._placeholder,
            id=pane_id(stream_id), classes="subagent-pane",
        )

    def set_usage_line(self, detail: str) -> None:
        self._usage_line.update(detail)
        self._usage_line.display = bool(detail)

    async def add(self, widget) -> None:
        """Mount a transcript child (sub-agent text or a nested tool call)."""
        await self.mount(widget)

    def append_error(self, report: str) -> None:
        """A failed spawn returns its error rather than streaming it; mount it so
        the transcript ends with the reason."""
        self.mount(Static(Content(report), classes="subagent-error"))

    def placeholder(self) -> None:
        """Show the 'no live transcript' note (a detached agent, pre-Phase 2)."""
        self._placeholder.display = True


class SubAgentDetailHost(ContentSwitcher):
    """A ``ContentSwitcher`` of ``SubAgentPane``s — the screen's right pane. One
    pane per ``stream_id``; ``current`` selects which is visible."""

    def add_pane(self, stream_id: str, agent_type: str, model_label: str) -> SubAgentPane:
        pane = SubAgentPane(stream_id, agent_type, model_label)
        # Hide the pane before mounting. ContentSwitcher only hides children present
        # at compose time, and watch_current only toggles the old/new pair on a
        # switch — it never hides the other dynamically-mounted panes. Without this,
        # every pane mounts visible (display defaults True) and they render stacked
        # instead of one-at-a-time. This mirrors Textual's own ContentSwitcher.
        # add_content, which sets display=False before mounting.
        pane.display = False
        self.mount(pane)
        return pane

    def pane(self, stream_id: str) -> "SubAgentPane | None":
        pid = pane_id(stream_id)
        for p in self.query(SubAgentPane):
            if p.id == pid:
                return p
        return None

    def show(self, stream_id: str) -> None:
        self.current = pane_id(stream_id)

    def current_sid(self) -> "str | None":
        for p in self.query(SubAgentPane):
            if p.id == self.current:
                return p.stream_id
        return None
