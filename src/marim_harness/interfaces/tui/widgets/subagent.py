"""The spawned sub-agent widget.

Inline in the log it is a *compact card*: a single-line header
``{glyph} {type} Task — {title}`` (a title derived from the prompt, clipped with an
ellipsis) plus an indented activity line. While the agent runs the activity line
shows the *current* tool (``↳ Read src/foo.py``); once it finishes it collapses to
the run summary (``↳ 45 toolcalls · 1m 18s``). The glyph animates while running
(✓/✕ when done). The card text is muted, brightening to white on hover. The
agent's full transcript streams into ``self.body``, a scroll container that
stays mounted but ``display:none`` — the full-screen viewer reveals it in
place by adding the ``viewing`` class (see ``subagent_viewer`` and the app's
``action_toggle_subagents``). Nothing is ever reparented, so a live stream keeps
mounting into the same container whether or not it is being viewed."""

import time

from textual.containers import Vertical, VerticalScroll
from textual.content import Content
from textual.widgets import Static

from .tool_summary import summarize

# Working-glyph animation frames (matches the status bar spinner) shown while the
# sub-agent is still running; a finished agent shows a static ✓/✕ instead.
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK = 0.1


def _fmt_duration(seconds: float) -> str:
    """Compact run duration: ``45s`` under a minute, else ``1m 18s``."""
    s = int(seconds)
    if s < 60:
        return f"{s}s"
    return f"{s // 60}m {s % 60}s"


_REASON_CAP = 70


def failure_reason(report: str) -> str:
    """The concise reason from a failed spawn's report — strips the
    ``Sub-agent 'x' failed: `` prefix (leaving the underlying error) and collapses
    whitespace; the full report stays available in the viewer transcript."""
    text = " ".join(report.split())
    marker = " failed: "
    if text.startswith("Sub-agent ") and marker in text:
        text = text.split(marker, 1)[1]
    return text if len(text) <= _REASON_CAP else text[: _REASON_CAP - 1] + "…"


# Boundaries a verbose spawn prompt is cut at to derive a card title — the first
# sentence/clause usually reads as a good title ("Provide a structural overview of
# the codebase. Include: …" → "Provide a structural overview of the codebase").
_TITLE_SEPS = (". ", ": ", "; ", " - ", " — ", "\n")
_TITLE_MAX = 80


def derive_title(task: str) -> str:
    """Condense a (possibly multi-paragraph) spawn prompt into a one-line title:
    take the text up to the first sentence/clause boundary, clipped to a sane
    length. Falls back to the whole (whitespace-collapsed) task when it has no such
    boundary; the card's CSS further clips it to the available width."""
    text = " ".join(task.split())
    cut = len(text)
    for sep in _TITLE_SEPS:
        i = text.find(sep)
        if 0 <= i < cut:
            cut = i
    title = text[:cut].rstrip(" .:;-—") or text
    if len(title) > _TITLE_MAX:
        title = title[: _TITLE_MAX - 1] + "…"
    return title or "(task)"


class SubAgentWidget(Vertical):
    """A spawned sub-agent rendered as a compact card. The header summarizes the
    delegation and live status; ``self.body`` holds the streamed transcript, hidden
    inline and revealed by the full-screen viewer."""

    def __init__(
        self, agent_type: str, agent_task: str, model_label: str = ""
    ) -> None:
        self.agent_type = agent_type
        self.agent_task = agent_task
        self.model_label = model_label
        # The owning stream's id (the spawn's tool_call_id); set by the renderer
        # once the widget is registered. The flush tick uses it to skip transcripts
        # that aren't currently being viewed.
        self.stream_id = ""
        self.status = "pending"  # "pending" | "done" | "denied" | "failed"
        self.report = ""
        self._fail_reason = ""
        # The current tool (humanized name + arg preview) shown on the ↳ line while
        # running; a tally + run timing replace it once finished. ``_t0`` is set at
        # mount and ``_t_end`` frozen at finish.
        self.activity = ""
        self.tool_count = 0
        self._t0 = time.monotonic()
        self._t_end: float | None = None
        # Live token usage. The total + cost ride on the card; the full cache split
        # is reserved for the body's muted header, where there's room for it.
        self.tokens = 0
        self.cost_text: str | None = None
        self.split_text = ""
        self._spin = 0
        # The card's two visible lines: a single-line header and the ↳ progress line.
        self._header = Static(classes="subagent-header")
        self._activity = Static(classes="subagent-activity")
        # The transcript home: a scroll container kept mounted but hidden inline.
        # A muted body header carries "{type} · {model}"; the usage line mirrors the
        # status bar's split + cost and stays hidden until metered. Transcript
        # widgets (text, tool calls) mount after these via add().
        self._body_header = Static(self._body_header_text(), classes="subagent-bhead")
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self.body = VerticalScroll(
            self._body_header, self._usage_line, classes="subagent-body"
        )
        self.body.display = False
        super().__init__(self._header, self._activity, self.body)

    def on_mount(self) -> None:
        self._paint_header()
        self._paint_activity()
        # Animate the working glyph and tick the duration while the agent runs; the
        # callback no-ops once the status leaves "pending", so a finished card stops
        # repainting.
        self.set_interval(_SPINNER_TICK, self._tick)

    # The CSS ``:hover`` pseudo-class only lands on the leaf widget under the
    # pointer, not on this container, so hovering a child line wouldn't light up
    # the whole card. Drive a ``-hovered`` class off the card's own mouse state
    # instead. Moving between the card's two lines fires Leave→Enter, so the leave
    # check is deferred until is_mouse_over has settled — otherwise the highlight
    # would flicker off as the pointer crosses from the header to the ↳ line.
    def on_enter(self, _event) -> None:
        self._sync_hover()

    def on_leave(self, _event) -> None:
        self.call_after_refresh(self._sync_hover)

    def _sync_hover(self) -> None:
        self.set_class(self.is_mouse_over, "-hovered")

    def _glyph(self) -> str:
        if self.status == "done":
            return "✓"
        if self.status in ("denied", "failed"):
            return "✕"
        return _SPINNER[self._spin]

    def display_title(self) -> str:
        """A concise one-line title derived from the (often verbose) spawn prompt —
        used on the card header and in the viewer's side-panel list."""
        return derive_title(self.agent_task)

    def _paint_header(self) -> None:
        # A derived title (not the raw prompt); CSS clips it with an ellipsis to the
        # card width. Content.assemble keeps the (untrusted) title a literal — never
        # markup-parsed — while tinting a failure glyph red so it reads at a glance.
        glyph_style = "red" if self.status in ("denied", "failed") else ""
        self._header.update(
            Content.assemble(
                (f"{self._glyph()} ", glyph_style),
                f"{self.agent_type} Task — {self.display_title()}",
            )
        )

    def _duration(self) -> str:
        end = self._t_end if self._t_end is not None else time.monotonic()
        return _fmt_duration(end - self._t0)

    def _paint_activity(self) -> None:
        if self.status == "pending":
            # Show the current tool while running; "working…" before the first call.
            self._activity.update(Content(f"↳ {self.activity or 'working…'}"))
        elif self.status in ("failed", "denied"):
            # Surface why it failed (literal + red); the full report is in the body.
            reason = self._fail_reason or ("denied" if self.status == "denied" else "failed")
            self._activity.update(Content.assemble((f"↳ {reason}", "red")))
        else:
            # Done: collapse to the run summary (tool tally + frozen duration).
            plural = "" if self.tool_count == 1 else "s"
            self._activity.update(
                Content(f"↳ {self.tool_count} toolcall{plural} · {self._duration()}")
            )

    def _body_header_text(self) -> Content:
        label = f"{self.agent_type} · {self.model_label}" if self.model_label else self.agent_type
        return Content(f"◼ {label}")

    def _tick(self) -> None:
        if self.status != "pending":
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        self._paint_header()  # advance the spinner glyph

    # --- live status updates (called by the stream renderer) ---

    def set_tokens(self, n: int) -> None:
        """Update the sub-agent's running token total."""
        self.tokens = n

    def set_usage(self, total: int, cost_text: str | None, split_text: str) -> None:
        """Fold a full usage reading in: the running ``total`` (and ``cost_text``)
        are kept for the card/footer; the detailed ``split_text`` + cost land in the
        body's muted header — the status-bar view, where there's room."""
        self.cost_text = cost_text
        self.split_text = split_text
        self.set_tokens(total)
        self._refresh_usage_line()

    def _refresh_usage_line(self) -> None:
        detail = self.split_text
        if self.cost_text:
            detail = f"{detail} · {self.cost_text}" if detail else self.cost_text
        self._usage_line.update(detail)
        self._usage_line.display = bool(detail)

    def note_tool(self, tool_name: str = "", args: dict | None = None) -> None:
        """Record that the sub-agent just called ``tool_name`` (with its ``args``):
        bump the tally and show it as the current tool on the ↳ line, using the same
        ``label · target  badges`` shape as the main log."""
        self.tool_count += 1
        s = summarize(tool_name, args or {}, cap=60)
        line = f"{s.label} · {s.target}" if s.target else s.label
        if s.badges:
            line = f"{line}  {' '.join(s.badges)}"
        self.activity = line
        self._paint_activity()

    def note_text(self) -> None:
        """The sub-agent is generating text. The card's progress line tracks tool
        tally + duration, so there's nothing to repaint here — kept for the renderer
        sink's interface."""

    async def add(self, widget) -> None:
        """Mount a transcript child (the sub-agent's text or a nested tool call)
        into the live body."""
        await self.body.mount(widget)

    def finish(self, report: str, status: str = "done") -> None:
        self.status = status
        self.report = report
        self._t_end = time.monotonic()  # freeze the duration
        if status in ("failed", "denied") and report:
            self._fail_reason = failure_reason(report)
            # The failure is returned, not streamed as an event, so the transcript
            # otherwise ends without it — append it so the viewer shows the reason.
            self.body.mount(Static(Content(report), classes="subagent-error"))
        self._paint_header()
        self._paint_activity()
