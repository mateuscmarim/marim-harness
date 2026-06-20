"""The spawned sub-agent widget: a collapsible whose title summarizes the
delegation (and live activity / token spend) and whose body streams the
sub-agent's own text and tool calls as they arrive."""

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Collapsible, Static

from .format import human_tokens


class SubAgentWidget(Collapsible):
    """A spawned sub-agent: the title summarizes the delegation; the (expanded)
    body is a live stream of the sub-agent's own text and tool calls, mounted as
    child widgets as its events arrive."""

    DEFAULT_CSS = """
    SubAgentWidget .subagent-usage {
        color: $text-muted;
    }
    """

    def __init__(
        self, agent_type: str, agent_task: str, collapsed: bool = False
    ) -> None:
        self.agent_type = agent_type
        self.agent_task = agent_task
        self.status = "pending"
        self.report = ""
        # Live activity shown in the (collapsed) title so a fan-out of agents is
        # legible at a glance without expanding each stream.
        self.activity = ""
        self.tool_count = 0
        # Live token usage. The total + cost ride in the (collapsed) title so a
        # fan-out exposes each agent's consumption at a glance; the full cache
        # split is reserved for the expanded body, where there's room for it.
        self.tokens = 0
        self.cost_text: str | None = None
        self.split_text = ""
        # A muted header line inside the expanded body carrying the detailed
        # split + cost (mirrors the session status bar). Hidden until populated
        # so an as-yet-unmetered agent doesn't show a blank line.
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self.body = Vertical(self._usage_line, classes="subagent-body")
        # title is a Content (not str) on purpose — see _summary.
        super().__init__(
            self.body, title=self._summary(), collapsed=collapsed  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        glyph = {"pending": "▸", "done": "✓", "denied": "✕"}.get(self.status, "▸")
        task = self.agent_task if len(self.agent_task) <= 40 else self.agent_task[:39] + "…"
        parts = [f"{glyph} spawn_agent({self.agent_type}: {task!r})"]
        # Only a running agent carries an activity tail; a finished one is clean.
        if self.status == "pending" and self.activity:
            parts.append(self.activity)
        # Token count and cost persist across finish — the final spend stays
        # visible. The three-way split is intentionally NOT here: it would bloat
        # the title and hurt fan-out legibility, so it lives in the body instead.
        if self.tokens:
            parts.append(f"{human_tokens(self.tokens)} tok")
        if self.cost_text:
            parts.append(self.cost_text)
        # Collapsible titles are parsed as Textual markup; the task text is
        # untrusted and may contain bracket sequences escape() can't neutralise,
        # so a literal Content bypasses markup parsing entirely.
        return Content(" · ".join(parts))

    def set_tokens(self, n: int) -> None:
        """Update the sub-agent's running token total and refresh the title."""
        self.tokens = n
        self.title = self._summary()

    def set_usage(self, total: int, cost_text: str | None, split_text: str) -> None:
        """Fold a full usage reading in: the title shows the running ``total`` (and
        ``cost_text`` when priced), while the expanded body's muted header shows the
        detailed ``split_text`` + cost — the status-bar view, where there's room."""
        self.cost_text = cost_text
        self.split_text = split_text
        self.set_tokens(total)  # updates the token total + repaints the title
        self._refresh_usage_line()

    def _refresh_usage_line(self) -> None:
        detail = self.split_text
        if self.cost_text:
            detail = f"{detail} · {self.cost_text}" if detail else self.cost_text
        self._usage_line.update(detail)
        self._usage_line.display = bool(detail)

    def note_tool(self, tool_name: str) -> None:
        """Record that the sub-agent just called ``tool_name`` and refresh the
        title — a cheap status update that needs no body mount."""
        self.tool_count += 1
        self.activity = f"{tool_name} ({self.tool_count})"
        self.title = self._summary()

    def note_text(self) -> None:
        """Record that the sub-agent is generating text and refresh the title."""
        self.activity = "responding"
        self.title = self._summary()

    async def add(self, widget) -> None:
        """Mount a child widget (the sub-agent's text or a nested tool call) into
        the live body."""
        await self.body.mount(widget)

    def finish(self, report: str, status: str = "done") -> None:
        self.status = status
        self.report = report
        self.activity = ""
        self.title = self._summary()
