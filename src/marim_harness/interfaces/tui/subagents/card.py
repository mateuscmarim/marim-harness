"""The spawned sub-agent widget.

Inline in the log it is a *compact card*: a single-line header
``{glyph} {type} Task — {title}`` (a title derived from the prompt, clipped with an
ellipsis) plus an indented activity line. While the agent runs the activity line
shows the *current* tool (``↳ Read src/foo.py``); once it finishes it collapses to
the run summary (``↳ 45 toolcalls · 1m 18s``). The glyph animates while running
(✓/✕ when done). The card text is muted, brightening to white on hover. The
agent's full transcript streams into a ``SubAgentPane`` owned by the detail host
(``SubAgentDetailHost``); the renderer attaches it to ``self.pane`` once both are
created, and scalar updates (usage/finish) redirect their body-side effects through
it. Nothing is ever reparented, so a live stream keeps mounting into the same pane
whether or not it is being viewed."""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Static

if TYPE_CHECKING:
    from pydantic_ai.usage import RunUsage

    from .pane import SubAgentPane

from ..widgets.tool_summary import summarize

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


_REASON_CAP = 160


def clean_reason(report: str) -> str:
    """The underlying error from a failed spawn's report — strips the
    ``Sub-agent 'x' failed: `` prefix (leaving the error itself) and collapses
    whitespace, but does NOT clip. This is the full text the card expands to."""
    text = " ".join(report.split())
    marker = " failed: "
    if text.startswith("Sub-agent ") and marker in text:
        text = text.split(marker, 1)[1]
    return text


def failure_reason(report: str) -> str:
    """The concise (clipped) reason for the card's collapsed line. The full,
    unclipped reason stays available via :func:`clean_reason` (the card expands to
    it on click) and in the viewer transcript."""
    text = clean_reason(report)
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
    delegation and live status; ``self.pane`` (set by the renderer) holds a reference
    to the ``SubAgentPane`` where the streamed transcript lives."""

    def __init__(
        self, agent_type: str, agent_task: str, model_label: str = "",
        description: str = "",
    ) -> None:
        self.agent_type = agent_type
        # The full spawn prompt — feeds the pane's "▸ task" disclosure verbatim.
        # Kept distinct from ``description`` so a short label never displaces it:
        # ``description`` is the optional 3-5 word title hint, ``agent_task`` the
        # real, often multi-paragraph, prompt the disclosure must reveal.
        self.agent_task = agent_task
        self.description = description
        self.model_label = model_label
        # Derived lazily from description-or-task (both fixed) and cached:
        # _paint_header asks for it on every spinner tick just to redraw one glyph,
        # so condensing the (often multi-paragraph) prompt per frame, ×N running
        # agents, is waste.
        self._title: str | None = None
        # The owning stream's id (the spawn's tool_call_id); set by the renderer
        # once the widget is registered. The flush tick uses it to skip transcripts
        # that aren't currently being viewed.
        self.stream_id = ""
        # The stream_id of the spawn that created this card, when it was spawned
        # by another sub-agent rather than the top-level agent. None for a
        # top-level spawn. Drives the depth-first tree order + connectors in the
        # sub-agents list (see stats.tree_order). Set by the renderer's
        # _claim_spawn at registration time.
        self.parent_id: str | None = None
        self.status = "pending"  # "pending" | "done" | "denied" | "failed" | "interrupted"
        self.report = ""
        self._fail_reason = ""  # clipped, shown on the collapsed card line
        self._full_reason = ""  # unclipped; the expanded line shows this
        # Click a failed card to expand its (clipped) error to the full body, and
        # back. Only meaningful when the reason was actually clipped.
        self._expanded = False
        # The current tool (humanized name + arg preview) shown on the ↳ line while
        # running; a tally + run timing replace it once finished. ``_t0`` is set at
        # mount and ``_t_end`` frozen at finish.
        self.activity = ""
        self.tool_count = 0
        # True when this spawn ran as a background job (Phase 2). It streams its
        # steps into this card like a foreground spawn, so the tally is real; the
        # flag only drives the quiet ``bg`` marker on the card header and list row.
        # Set by the renderer when the card is mapped to a background job
        # (note_detached_spawn).
        self.detached = False
        # after= dependency display (spec 2026-07-02-after-deps-tui-design).
        # ``after_ids`` are the prerequisite background-job ids from the spawn's
        # tool args; ``job_id`` is this card's own background job (parsed from
        # the detach handoff); ``waiting`` is DERIVED display state — status
        # stays "pending", so nothing that switches on status changes; and
        # ``blocked_by`` names the failed prerequisite once one kills the run.
        # All set post-construction by the renderer, like stream_id/parent_id.
        self.after_ids: list[str] = []
        self.job_id: str | None = None
        self.waiting = False
        self.blocked_by: str | None = None
        self._t0 = time.monotonic()
        self._t_end: float | None = None
        # A resumed card's real run duration, restored from its sidecar meta.
        # _t0/_t_end measure *replay* wall-clock on a resumed session (both stamp
        # at settle time → "0s"), so _duration() prefers this when set.
        self._restored_duration: float | None = None
        # Live token usage. The total + cost ride on the card; the full cache split
        # is forwarded to the pane's usage line, where there's room for it.
        self.tokens = 0
        self.cost_text: str | None = None
        self.split_text = ""
        self._spin = 0
        # The card's two visible lines: a single-line header and the ↳ progress line.
        self._header = Static(classes="subagent-header")
        self._activity = Static(classes="subagent-activity")
        # The transcript no longer lives on the card — it streams into a
        # SubAgentPane owned by the detail host. The renderer attaches that pane
        # here once both are created; scalar updates (usage/finish) redirect their
        # body-side effects through it. None until attached, and stays None for the
        # pure card unit tests, so every access guards on it.
        self.pane: SubAgentPane | None = None
        # The numeric cost of this agent's run, folded in by set_usage; the summary
        # bar sums these (rather than re-parsing the formatted cost_text).
        self.cost_value: float | None = None
        # Latest live RunUsage stashed by the renderer, priced on the next flush
        # tick rather than inline per delta (see StreamRenderer._drain_subagent_usage).
        # _priced_tokens is the token total at the last pricing, so a tick can skip a
        # card whose usage hasn't moved. -1 forces the first pricing.
        self._pending_usage: RunUsage | None = None
        self._priced_tokens = -1
        super().__init__(self._header, self._activity)

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

    def on_click(self, _event) -> None:
        # Click a failed card to expand the clipped error to its full body, and
        # back. A no-op unless the reason was actually clipped (so a fully-shown
        # error doesn't react to clicks).
        if self.status in ("failed", "denied") and self._full_reason != self._fail_reason:
            self._expanded = not self._expanded
            self._paint_activity()
            return
        # Otherwise, a click jumps into the sub-agents screen focused on this card.
        # Guarded with getattr so bare-App test harnesses (no HarnessApp.subagents)
        # treat a click as a no-op.
        viewer = getattr(self.app, "subagents", None)
        if viewer is not None:
            viewer.open_at(self.stream_id)

    def _glyph(self) -> str:
        if self.status == "done":
            return "✓"
        if self.status in ("denied", "failed"):
            return "✕"
        if self.status == "interrupted":
            return "⏸"
        if self.waiting:
            return "⧗"
        return _SPINNER[self._spin]

    def display_title(self) -> str:
        """A concise one-line title for the card header and the viewer's side-panel
        list: the caller's short ``description`` when given, else one derived from
        the (often verbose) spawn prompt. Derived once and cached: both inputs are
        fixed, but this is called per spinner tick."""
        if self._title is None:
            self._title = derive_title(self.description or self.agent_task)
        return self._title

    def set_model(self, model_label: str) -> None:
        """Record the real model this sub-agent ran on (e.g. the model a claude-cli
        spawn reported), overriding the harness-model fallback set at creation. The
        model isn't shown on the card itself; storing it here means a pane created
        later picks it up, and the renderer also pushes it to an already-open pane.
        Also stores a pricing-compatible id by stripping the [1m]-style context-window
        suffix the Claude CLI appends, which isn't present in the price table."""
        self.model_label = model_label

    def set_waiting(self, waiting: bool) -> None:
        """Flip the derived waiting display state (an after= spawn blocked on
        prerequisites) and repaint both card lines. Display-only: ``status``
        stays "pending". No-op when unchanged, so jobs-change sweeps can call
        it unconditionally."""
        if self.waiting == waiting:
            return
        self.waiting = waiting
        self._paint_header()
        self._paint_activity()

    def _paint_header(self) -> None:
        # A derived title (not the raw prompt); CSS clips it with an ellipsis to the
        # card width. Content.assemble keeps the (untrusted) title a literal — never
        # markup-parsed — while tinting a failure glyph red so it reads at a glance.
        # A background (detached) spawn carries a dim ``bg`` tag so an off-turn agent
        # is tellable from one running inside the current turn.
        glyph_style = "red" if self.status in ("denied", "failed") else ""
        parts: list = [(f"{self._glyph()} ", glyph_style)]
        if self.detached:
            parts.append(("bg ", "dim"))
        if self.waiting and self.after_ids:
            parts.append((f"after {', '.join(self.after_ids)} ", "dim"))
        elif self.blocked_by:
            parts.append((f"blocked by {self.blocked_by} ", "dim red"))
        parts.append(f"{self.agent_type} Task — {self.display_title()}")
        self._header.update(Content.assemble(*parts))

    def _duration(self) -> str:
        if self._restored_duration is not None:
            return _fmt_duration(self._restored_duration)
        end = self._t_end if self._t_end is not None else time.monotonic()
        return _fmt_duration(end - self._t0)

    def _paint_activity(self) -> None:
        if self.status == "pending":
            if self.waiting and self.after_ids:
                # Blocked on prerequisites: say so instead of "working…", so a
                # stalled dependent is tellable from a busy one at a glance.
                self._activity.update(
                    Content(f"↳ waiting on {', '.join(self.after_ids)}")
                )
            else:
                # Show the current tool while running; "working…" before the
                # first call.
                self._activity.update(Content(f"↳ {self.activity or 'working…'}"))
        elif self.status in ("failed", "denied"):
            # Surface why it failed (literal + red). The line is clipped to one row
            # by default; if the reason was clipped, a ▸/▾ marks it click-to-expand
            # (the full body also lives in the viewer transcript).
            expandable = self._full_reason != self._fail_reason
            if self._expanded and expandable:
                reason = self._full_reason
            else:
                reason = self._fail_reason or (
                    "denied" if self.status == "denied" else "failed"
                )
            marker = ("  ▾" if self._expanded else "  ▸") if expandable else ""
            # Let the line grow + wrap only while expanded; otherwise it stays one row.
            self._activity.set_class(self._expanded and expandable, "-expanded")
            self._activity.update(Content.assemble((f"↳ {reason}", "red"), (marker, "dim")))
        elif self.status == "interrupted":
            self._activity.update(Content.assemble(
                ("↳ interrupted — press r in the sub-agents screen (ctrl+x) to resume",
                 "dim"),
            ))
        else:
            # Done: collapse to the run summary (tool tally + frozen duration). A
            # background agent streams its steps too, so its tally is real.
            plural = "" if self.tool_count == 1 else "s"
            self._activity.update(
                Content(f"↳ {self.tool_count} toolcall{plural} · {self._duration()}")
            )

    def _tick(self) -> None:
        if self.status != "pending":
            return
        self._spin = (self._spin + 1) % len(_SPINNER)
        self._paint_header()  # advance the spinner glyph

    # --- live status updates (called by the stream renderer) ---

    def set_tokens(self, n: int) -> None:
        """Update the sub-agent's running token total."""
        self.tokens = n

    def set_usage(
        self, total: int, cost_text: str | None, split_text: str,
        cost_value: float | None = None,
    ) -> None:
        """Fold a full usage reading in: the running ``total`` (and ``cost_text``)
        ride on the card for the list row; ``cost_value`` (numeric) feeds the
        summary roll-up; the detailed ``split_text`` + cost land on the pane's usage
        line (the status-bar view, where there's room)."""
        self.cost_text = cost_text
        self.cost_value = cost_value
        self.split_text = split_text
        self.set_tokens(total)
        if self.pane is not None:
            detail = split_text
            if cost_text:
                detail = f"{detail} · {cost_text}" if detail else cost_text
            self.pane.set_usage_line(detail)

    def restore_stats(self, tool_count: int = 0, tokens: int = 0,
                      duration: float | None = None) -> None:
        """Rehydrate the run stats a resumed spawn's sidecar meta persisted
        (see the runner's _final_meta): the tool tally and token total the live
        stream would have accumulated, and the real run duration to show instead
        of the meaningless replay-epoch _t0/_t_end delta. Cost is deliberately
        not restored — pricing needs the model catalog, and a stale price is
        worse than a blank cell."""
        self.tool_count = tool_count
        self.set_tokens(tokens)
        self._restored_duration = duration

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

    def note_retry(self, message: str) -> None:
        """Show a transient-error retry on the ↳ line (e.g. after a 504 gateway
        timeout) so a recovering spawn reads as working, not stalled. The next tool
        call or text event overwrites it once the retried run gets going again."""
        self.activity = f"⟳ {message}"
        self._paint_activity()

    def note_text(self) -> None:
        """The sub-agent is generating text. The card's progress line tracks tool
        tally + duration, so there's nothing to repaint here — kept for the renderer
        sink's interface."""

    def finish(self, report: str, status: str = "done") -> None:
        # A terminal card never shows the waiting glyph/tag, whatever ordering
        # the settle events arrived in — finish() repaints both lines anyway.
        self.waiting = False
        self.status = status
        self.report = report
        self._t_end = time.monotonic()  # freeze the duration
        if status in ("failed", "denied") and report:
            self._fail_reason = failure_reason(report)
            self._full_reason = clean_reason(report)
            # The failure is returned, not streamed, so the transcript would
            # otherwise end without it — append it to the pane so the screen shows
            # the reason. Guard: a detached/pre-pane card has no pane yet.
            if self.pane is not None:
                self.pane.append_error(report)
        self._paint_header()
        self._paint_activity()
