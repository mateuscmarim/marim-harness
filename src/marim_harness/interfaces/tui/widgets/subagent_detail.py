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


def _short_model(model_label: str) -> str:
    """The compact model name for the pane header: the last path segment of a
    routed id (``openrouter/openrouter/owl-alpha`` → ``owl-alpha``,
    ``openrouter/anthropic/claude-sonnet-4-6`` → ``claude-sonnet-4-6``). The pane
    now carries the agent's title to identify it, so the bare model name reads
    cleaner here than the full provider-routed label (which still shows on the
    status bar)."""
    return model_label.rsplit("/", 1)[-1] if model_label else ""


class SubAgentPane(VerticalScroll):
    """One sub-agent's transcript: a muted ``◼ {type} · {model}`` header, an
    (initially hidden) usage line mirroring the status bar's split + cost, and the
    streamed transcript widgets mounted after them. Replaces the old
    ``SubAgentWidget.body``."""

    def __init__(self, stream_id: str, agent_type: str, model_label: str,
                 title: str = "", task: str = "") -> None:
        self.stream_id = stream_id
        # Kept so set_model can rebuild the header/subtitle when the real model
        # arrives after construction (a claude-cli spawn reports it mid-stream).
        self._agent_type = agent_type
        self._title = title
        model = _short_model(model_label)
        context = f"{agent_type} · {model}" if model else agent_type
        # Line 1 (headline): the agent's description/title with the ◼ glyph — the
        # most useful identifier when several same-type agents are spawned. Falls
        # back to the type · model context for a bare pane with no title.
        self._header = Static(Content(f"◼ {title or context}"), classes="subagent-bhead")
        # Line 2 (subtitle): the type · model context, muted. Shown only when line 1
        # carries a title — otherwise it would just repeat the header. (Named
        # ``_subhead`` rather than ``_context`` — the latter shadows a Textual
        # Widget internal.)
        self._subhead = Static(Content(context), classes="subagent-bsub")
        self._subhead.display = bool(title)
        # Collapsible disclosure for the full prompt the agent was given (the
        # headline is only a derived one-line title). A muted ``▸ task`` line that
        # expands to the full text on click or the screen's ``t`` key, so the
        # default view stays clean. Hidden entirely when there's no task.
        # (Named ``_task_text`` — ``_task`` shadows a Textual Widget internal.)
        self._task_text = task
        self._task_expanded = False
        self._task_toggle = Static(Content("▸ task"), classes="subagent-task-toggle")
        self._task_toggle.display = bool(task)
        self._task_body = Static(Content(task), classes="subagent-task-body")
        self._task_body.display = False
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self._placeholder = Static(Content(_DETACHED_NOTE), classes="subagent-detached")
        self._placeholder.display = False
        super().__init__(
            self._header, self._subhead, self._task_toggle, self._task_body,
            self._usage_line, self._placeholder,
            id=pane_id(stream_id), classes="subagent-pane",
        )

    def toggle_task(self) -> None:
        """Expand/collapse the full-task disclosure. A no-op when there's no task
        (the toggle line is hidden in that case)."""
        if not self._task_text:
            return
        self._task_expanded = not self._task_expanded
        self._task_toggle.update(Content("▾ task" if self._task_expanded else "▸ task"))
        # When expanded the body below carries the bottom gap, so drop the toggle's
        # own margin to keep '▾ task' hugging its text (mirrors the card's -expanded).
        self._task_toggle.set_class(self._task_expanded, "-expanded")
        self._task_body.display = self._task_expanded

    def on_click(self, event) -> None:
        """Clicking the ``▸ task`` line toggles the disclosure — the same
        click-to-expand idiom the failed-spawn card uses. Clicks elsewhere in the
        pane (the transcript) bubble through untouched."""
        if getattr(event, "widget", None) is self._task_toggle:
            self.toggle_task()

    def set_model(self, model_label: str) -> None:
        """Replace the model shown in the subtitle (``type · model``) once the real
        model is known — e.g. a claude-cli spawn reports its model mid-stream, where
        the pane was created with the harness's fallback model. Rebuilds the muted
        subtitle, and the headline too when it has no title to show instead."""
        model = _short_model(model_label)
        context = f"{self._agent_type} · {model}" if model else self._agent_type
        self._subhead.update(Content(context))
        if not self._title:
            self._header.update(Content(f"◼ {context}"))

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

    def add_pane(self, stream_id: str, agent_type: str, model_label: str,
                 title: str = "", task: str = "") -> SubAgentPane:
        pane = SubAgentPane(stream_id, agent_type, model_label, title, task)
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
