"""The event→widget streaming engine — extracted from HarnessApp.

Turns a turn's (and each sub-agent's) streamed events into the log's live
AssistantMessage / ToolCallWidget / SubAgentWidget tree. Owns all per-turn stream
state; reaches the app and the status presenter through ``self.app``."""

import abc
import re
import time
from dataclasses import dataclass, field
from typing import cast

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
)
from textual.containers import VerticalScroll
from textual.widget import Widget

from ...usage import resolve_cost
from .subagents import SubAgentDetailHost, SubAgentPane, SubAgentWidget  # noqa: F401
from .widgets import (
    AssistantMessage,
    ThinkingWidget,
    ToolCallWidget,
    ToolGroupWidget,
)
from .widgets import format_cost as _format_cost
from .widgets import format_token_split as _format_token_split


def status_from_part(part) -> str:
    """Map a tool-result part to a ToolCallWidget status. A ``ToolReturnPart``
    carries an ``outcome`` of 'success'/'failed'/'denied' (pydantic-ai sets
    'denied' when an approval round rejects the call); a ``RetryPromptPart`` has
    no outcome and represents a validation/ModelRetry failure. Without this the
    widget defaulted every result to 'done', so a denied write_file rendered a
    green ✓ instead of the ✕ the widget was built to show."""
    outcome = getattr(part, "outcome", None)
    if outcome == "denied":
        return "denied"
    if outcome == "failed":
        return "failed"
    if getattr(part, "part_kind", None) == "retry-prompt":
        return "failed"
    return "done"


# Prefixes of the strings a failed foreground spawn *returns* (it contains its
# error rather than raising, so the spawn_agent tool call's outcome is still
# "success"). The card would otherwise render a ✓; matching these flips it to ✕.
# Tool-level after= rejections are included so a refused dependent renders ✕, not a green ✓.
_SUBAGENT_FAIL_PREFIXES = (
    "No sub-agent type ",
    "Can't run sub-agent ",
    "Failed to build sub-agent",
    "Isolated spawn needs ",
    "Couldn't create an isolated worktree",
    "Cannot spawn with after=",
    "after= requires a detached spawn",
)


def subagent_failed(content: str) -> bool:
    """True when a spawn's returned text is one of the runner's failure messages —
    so a contained failure (which the tool reports as a successful return) still
    renders the card as failed."""
    text = content.lstrip()
    if text.startswith("Sub-agent ") and " failed:" in text[:120]:
        return True
    return text.startswith(_SUBAGENT_FAIL_PREFIXES)


_DETACH_PREFIX = "Started detached sub-agent "
_BG_PREFIX = "Started "
_BG_AGENT_MARK = " (agent)"


def _detached_job_id(content: str) -> str | None:
    """The background job id behind an agent spawn's return, or None for any
    other tool return. Recognizes both producers so the card fills for either
    detach path: an *auto-detached* handoff (``_detach_handoff`` →
    ``"Started detached sub-agent <id>, …"``) and an *explicit* ``background=True``
    spawn (``spawn_tools.spawn_agent`` → ``"Started <id> (agent) — <label>"``). A bash
    background job (``"Started <id> (bash) …"``) is intentionally not matched — it's
    not a sub-agent card. Round-trip tests pin both formats."""
    text = content.lstrip()
    if text.startswith(_DETACH_PREFIX):
        job_id, sep, _ = text[len(_DETACH_PREFIX):].partition(",")
        return job_id.strip() if sep and job_id.strip() else None
    if text.startswith(_BG_PREFIX):
        rest = text[len(_BG_PREFIX):]
        idx = rest.find(_BG_AGENT_MARK)
        if idx > 0:
            return rest[:idx].strip() or None
    return None


def _wait_subagent_label(args: dict, jobs) -> str | None:
    """The sub-agent display label (``f"{type}: {task}"``, the job label) a
    ``wait_for_job`` is blocking on, or None when the waited job isn't a sub-agent
    (a bash job, or no such job). Used to name the wait row — "Wait · <task>" —
    rather than show a bare job id. Returned for any status: the spawn owns the
    sub-agent card, so the wait is just a thin labelled row, with no card-vs-timeout
    concern that would need the job to be finished."""
    job = jobs.get(str(args.get("id", "")))
    if job is None or job.kind != "agent":
        return None
    return job.label or None


def _after_ids(args: dict) -> list[str]:
    """The spawn's after= prerequisite job ids from its tool args, normalized
    (str → 1-list; entries stripped, empties dropped). Local on purpose — the
    tool layer has its own normalizer, but the TUI shouldn't import tools."""
    raw = args.get("after")
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list):
        return []
    return [s for s in (str(x).strip() for x in raw) if s]


def _deps_pending(after_ids: list[str], jobs) -> bool:
    """True while any prerequisite job is still running. A missing/pruned id
    counts as settled so a card can never block forever on a forgotten job —
    mirrors JobRegistry.await_settled's semantics for display purposes."""
    return any(
        (job := jobs.get(jid)) is not None and job.status == "running"
        for jid in after_ids
    )


_PREREQ_RE = re.compile(r"prerequisite (job-\d+) (?:failed|cancelled|no longer exists)")


def blocked_by_id(content: str) -> str | None:
    """The culprit job id from a PrerequisiteFailed report, or None. Matches the
    message _run_after raises ("prerequisite job-3 failed — …"), which may reach
    the card prefixed by the exception class name; only the head is scanned so a
    report that merely quotes the phrase deep in its body doesn't match."""
    m = _PREREQ_RE.search(content[:300])
    return m.group(1) if m else None


def _stream_hidden(widget: Widget, host: "SubAgentDetailHost | None") -> bool:
    """True when ``widget`` is a sub-agent transcript that isn't the one currently
    on screen, so re-parsing its markdown every flush tick would be wasted work
    (and, ×N during a fan-out, would freeze the UI). A widget inside a SubAgentPane
    is hidden unless that pane is the host's current one; a top-level log stream
    (no pane ancestor) is never hidden. With no host, or no pane selected (screen
    closed), every sub-agent stream is hidden — they render the moment a pane is
    shown."""
    node = widget.parent
    while node is not None:
        if isinstance(node, SubAgentPane):
            return host is None or node.id != host.current
        node = node.parent
    return False


@dataclass
class _SubStreamState:
    """Per-stream state for one nested sub-agent — replaces four parallel dicts."""
    group: "ToolGroupWidget | None" = field(default=None)
    solo: "ToolCallWidget | None" = field(default=None)
    assistant: "AssistantMessage | None" = field(default=None)
    thinking: "ThinkingWidget | None" = field(default=None)


class _StreamSink(abc.ABC):
    """Where one event stream's widgets land and how its run-state is read/written.

    Routing a streamed turn is identical whether the events come from the
    top-level agent or a nested sub-agent — the only things that differ are the
    mount container, where this stream's run-state and current assistant message
    live, the title bookkeeping, and whether a tool call gets intercepted (the
    spawn_agent special case). A sink captures exactly those, so one dispatch core
    (:meth:`StreamRenderer.dispatch_stream_event`) serves both. Hooks default to
    no-ops; sub-classes override only what their scope needs."""

    # Mount target for assistant text and bare tool widgets. Honestly Optional: a
    # sub-agent sink whose detail pane isn't mounted (headless, or an early race)
    # has no container. ``dispatch_stream_event`` guards on None once at entry and
    # threads the narrowed Widget into the handlers that mount, so the None case
    # is handled at the boundary rather than papered over with a cast.
    container: Widget | None
    # The owning renderer, set by every concrete sink's __init__. Declared here so
    # shared base-class helpers (``_claim_spawn``) can reach it under the type
    # checker without each subclass re-declaring the attribute.
    _r: "StreamRenderer"

    @abc.abstractmethod
    def get_run(self) -> tuple:
        """This stream's (group, solo) run-of-consecutive-tools state."""

    @abc.abstractmethod
    def set_run(self, group, solo) -> None: ...

    @abc.abstractmethod
    def get_assistant(self) -> "AssistantMessage | None": ...

    @abc.abstractmethod
    def set_assistant(self, msg) -> None: ...

    @abc.abstractmethod
    def get_thinking(self) -> "ThinkingWidget | None": ...

    @abc.abstractmethod
    def set_thinking(self, widget) -> None: ...

    def on_text(self) -> None:  # noqa: B027
        """Called when the stream starts a text part (title status, sub only)."""

    def on_tool(self, tool_name: str, args: dict) -> None:  # noqa: B027
        """Called when the stream makes a tool call (card status, sub only)."""

    async def intercept_tool(self, event, args: dict, container: Widget) -> bool:
        """Give the scope first refusal on a tool call; return True to claim it and
        skip the default ToolCallWidget path. ``container`` is the dispatch-narrowed
        (non-None) mount target. Default: never intercepts."""
        return False

    async def _claim_spawn(
        self, event, args: dict, container: Widget, parent_id: str | None
    ) -> "SubAgentWidget":
        """Shared spawn_agent claim for both scopes: build the live card, register
        it so its own stream (forwarded by the runner under this tool_call_id) can
        find it, create its detail pane, break the current tool run, and mount the
        card into this sink's container (#log for the top-level agent, the parent's
        pane for a nested spawn). ``parent_id`` tags the card for the list's tree
        order (None for a top-level spawn). Returns the card; both call sites
        currently ignore it (exposed for tests and future callers)."""
        widget = self._r.mount_spawn_widget(args)
        widget.stream_id = event.part.tool_call_id
        widget.parent_id = parent_id
        self._r.tool_widgets[event.part.tool_call_id] = widget
        self._r.ensure_pane(widget)
        self.set_run(None, None)
        await container.mount(widget)
        return widget

    def on_result(self, event) -> None:  # noqa: B027
        """Called after a tool result is rendered (cleanup hook)."""


class _TopLevelSink(_StreamSink):
    """The top-level turn stream: mounts into the main log, keeps run-state and the
    current assistant on the renderer's scalar fields, and claims foreground
    spawn_agent calls so they render as a live SubAgentWidget instead of a generic
    tool."""

    # The main log is always mounted, so the top-level sink's container is never
    # None in practice; the mount sites still receive the dispatch-narrowed Widget
    # by parameter rather than relying on a covariant attribute override (which a
    # mutable attribute can't express).
    def __init__(self, renderer: "StreamRenderer", container: Widget) -> None:
        self._r = renderer
        self.container = container

    def get_run(self) -> tuple:
        return self._r.tool_group, self._r.solo_tool

    def set_run(self, group, solo) -> None:
        self._r.tool_group = group
        self._r.solo_tool = solo

    def get_assistant(self) -> AssistantMessage | None:
        return self._r.current_assistant

    def set_assistant(self, msg) -> None:
        self._r.current_assistant = msg

    def get_thinking(self) -> ThinkingWidget | None:
        return self._r.current_thinking

    def set_thinking(self, widget) -> None:
        self._r.current_thinking = widget

    async def intercept_tool(self, event, args: dict, container: Widget) -> bool:
        # Every spawn_agent gets a live SubAgentWidget, mounted standalone so it
        # isn't buried in a tool group. A foreground spawn streams its steps into
        # the card; a background/detached spawn (auto or explicit background=True)
        # returns a job-id handoff that holds the card pending until the job
        # settles and fills it (note_detached_spawn / fill_finished_detached_cards)
        # — so a backgrounded spawn no longer renders a misleading ✓ tool row.
        if event.part.tool_name == "spawn_agent":
            await self._claim_spawn(event, args, container, parent_id=None)
            return True
        # ask_user is a user-facing Q&A, not mechanical work — keep it out of the
        # collapsed tool group, where the question and the user's answer would be
        # hidden behind a "≡ N tools" fold. Render a normal tool widget but mount
        # it standalone and break the run on both sides (same rationale as the
        # foreground spawn_agent case above).
        if event.part.tool_name == "ask_user":
            widget = ToolCallWidget(
                event.part.tool_name, args,
                workspace_root=self._r.app.harness.deps.workspace.root,
            )
            self._r.tool_widgets[event.part.tool_call_id] = widget
            self.set_run(None, None)
            await container.mount(widget)
            return True
        return False

    def on_result(self, event) -> None:
        # A foreground spawn's stream_id is its tool_call_id; drop its sub-stream
        # state once the spawn returns. The sub-agent's final text block is the last
        # thing it streamed, so no later event ever finalized it (the per-event
        # finalize in dispatch fires on the *next* event) — finalize it here so a
        # busy-fan-out streaming-duplication is healed to a clean reparse before the
        # card settles. See AssistantMessage.finalize.
        state = self._r._sub_streams.pop(event.tool_call_id, None)
        if state is not None and state.assistant is not None:
            state.assistant.finalize()


class _SubAgentSink(_StreamSink):
    """A nested sub-agent stream: mounts into the agent's pane in the detail host,
    keeps run-state and the current assistant in per-stream dicts keyed by
    ``stream_id``, and pushes live text/tool activity into the (collapsed) widget
    title."""

    def __init__(self, renderer: "StreamRenderer", parent: SubAgentWidget,
                 stream_id: str) -> None:
        self._r = renderer
        self._parent = parent
        self._sid = stream_id
        # Mount transcript widgets into the agent's pane in the detail host. The
        # pane is created at spawn; ensure_pane is idempotent and covers the rare
        # race where the sink runs before intercept_tool attached it. Genuinely
        # None when the host isn't mounted (headless); dispatch_stream_event guards
        # on None and skips, so the Optional rides through honestly (no cast).
        self.container = renderer.ensure_pane(parent)

    def get_run(self) -> tuple:
        state = self._r._sub_streams.get(self._sid)
        return (state.group, state.solo) if state is not None else (None, None)

    def set_run(self, group, solo) -> None:
        state = self._r._sub_streams.setdefault(self._sid, _SubStreamState())
        state.group = group
        state.solo = solo

    def get_assistant(self) -> AssistantMessage | None:
        state = self._r._sub_streams.get(self._sid)
        return state.assistant if state is not None else None

    def set_assistant(self, msg) -> None:
        self._r._sub_streams.setdefault(self._sid, _SubStreamState()).assistant = msg

    def get_thinking(self) -> ThinkingWidget | None:
        state = self._r._sub_streams.get(self._sid)
        return state.thinking if state is not None else None

    def set_thinking(self, widget) -> None:
        self._r._sub_streams.setdefault(self._sid, _SubStreamState()).thinking = widget

    async def intercept_tool(self, event, args: dict, container: Widget) -> bool:
        # A nested spawn_agent gets the same live card as a top-level one, mounted
        # into this sub-agent's pane and tagged with this agent as its parent. The
        # child's own stream is already forwarded by the runner under the nested
        # spawn's tool_call_id (subagents/runner.py); registering the card here is
        # what lets on_subagent_event find it instead of dropping the stream.
        if event.part.tool_name == "spawn_agent":
            await self._claim_spawn(
                event, args, container, parent_id=self._parent.stream_id
            )
            return True
        return False

    def on_text(self) -> None:
        self._parent.note_text()

    def on_tool(self, tool_name: str, args: dict) -> None:
        self._parent.note_tool(tool_name, args)


class StreamRenderer:
    """Renders streamed turn/sub-agent events into the log; owns the per-turn
    widget/run state. Holds the HarnessApp as ``app`` for DOM access and the status
    presenter (``app.status``)."""

    def __init__(self, app) -> None:
        self.app = app
        self.current_assistant: AssistantMessage | None = None
        self.current_thinking: ThinkingWidget | None = None
        self.tool_widgets: dict[str, ToolCallWidget | SubAgentWidget] = {}
        self.tool_group: ToolGroupWidget | None = None
        self.solo_tool: ToolCallWidget | None = None
        self._sub_streams: dict[str, _SubStreamState] = {}
        # Every foreground sub-agent spawned this session, in spawn order — the
        # backing list for the sub-agents screen's list/navigation and the
        # summary roll-up.
        self.subagents: list[SubAgentWidget] = []
        # job_id → a pending detached-spawn card, awaiting its background job's
        # report. Filled on job settle (fill_finished_detached_cards); cleared on
        # session reset. Not pruned per-turn: the job finishes after the turn ends.
        self._detached_cards: dict[str, SubAgentWidget] = {}
        # The persistent transcript host (a ContentSwitcher of SubAgentPanes), set
        # by the app at mount. Panes are created here per spawn and attached to
        # their card; the sub-agent sink mounts each stream into its pane.
        self.detail_host: SubAgentDetailHost | None = None
        # Either an AssistantMessage (reply/sub-agent body) or a ThinkingWidget —
        # both expose the append/flush streaming interface the tick drains.
        self.dirty_streams: set[AssistantMessage | ThinkingWidget] = set()
        # Sub-agent cards that took new live usage since the last frame; priced once
        # per flush tick rather than inline per delta (see _drain_subagent_usage).
        self.dirty_usage_cards: set[SubAgentWidget] = set()
        self.live_run_tokens = 0
        # Time-to-first-token of the most recent model request (seconds), or
        # None before the first stream / after a session reset. Set through
        # bind_ui's on_ttft by the controller's TtftTrackingModel wrapper —
        # NOT measured in on_events: pydantic-ai waits for the first chunk
        # while opening the stream, before the handler is ever invoked, so a
        # handler-side timestamp always reads ~0 (see runtime/ttft.py).
        self.last_ttft: float | None = None
        self.show_all_output = False  # Ctrl+O reveal-all toggle
        # True while a session view is being rebuilt (clear/switch/new). During the
        # rebuild the log's max_scroll_y is briefly stale (old content removed, new
        # not laid out), so an interval flush tick must not anchor off it.
        self.rebuilding = False
        # Latch: True once we've anchored the log on its first overflow. After that
        # we never force the anchor again — Textual releases it on a user scroll-up
        # and re-engages at the bottom on its own. Seeded by the view that
        # establishes the anchor state (mount / render_session).
        self._anchored_on_overflow = False

    def reset(self) -> None:
        """Clear per-session stream state when the log is rebuilt (new/switch/clear)."""
        # The rebuilt view re-establishes its own anchor state, so drop the latch;
        # render_session re-seeds it to match the anchor it sets.
        self._anchored_on_overflow = False
        self.current_assistant = None
        self.current_thinking = None
        self.tool_widgets.clear()
        self.tool_group = None
        self.solo_tool = None
        self._sub_streams.clear()
        self.subagents.clear()
        self._detached_cards.clear()
        self.dirty_streams.clear()
        self.last_ttft = None

    def on_ttft(self, seconds: float) -> None:
        """bind_ui callback: record the latest streamed request's TTFT. The
        status bar picks it up on its next repaint tick; no eager refresh."""
        self.last_ttft = seconds

    def adopt_resumed_card(self, card: "SubAgentWidget", job_id: str) -> None:
        """Re-arm an interrupted-or-still-live card onto background job
        ``job_id``: flip it live, route the resumed run's stream back into it,
        and map the job so the settle fills it like any detached spawn. Called
        both when a spawn is just resumed and, on the settle path, to re-arm a
        still-running job's card during replay (``session_view.
        finish_replayed_cards``)."""
        card.status = "pending"
        card._t0 = time.monotonic()
        card._t_end = None
        card.detached = True
        card.job_id = job_id
        self.tool_widgets[card.stream_id] = card
        self._detached_cards[job_id] = card
        card._paint_header()
        card._paint_activity()

    def note_detached_spawn(self, content: str, widget: "SubAgentWidget", jobs) -> bool:
        """If ``content`` is a detached-spawn handoff, mark the card as a background
        run and map its job_id → card so it settles when the job finishes; return
        True so the caller does NOT finish the card on the handoff text (which is a
        job-id handoff, not the report). Returns False for a normal report, so
        foreground spawns and wait_for_job cards finish as usual.

        Phase 2: a background sub-agent streams its transcript into this card's pane
        live (its run is wired with this spawn's stream_id), so the card shows real
        activity — no 'no live transcript' placeholder. ``widget.detached`` here
        means 'ran as a background job' and drives only the quiet ``bg`` marker on
        the card and list row. Fills at once if the job already settled (a fast job
        can finish before its handoff renders). An after= dependent also records
        its own job id and enters the derived *waiting* display state while any
        prerequisite still runs (spec 2026-07-02-after-deps-tui-design)."""
        job_id = _detached_job_id(content)
        if job_id is None:
            return False
        widget.detached = True  # bg marker; the live stream fills the tally + pane
        widget.job_id = job_id
        if widget.after_ids and _deps_pending(widget.after_ids, jobs):
            widget.set_waiting(True)
        self._detached_cards[job_id] = widget
        self._fill_detached_card(job_id, jobs)
        return True

    def _fill_detached_card(self, job_id: str, jobs) -> None:
        """Finish the mapped card for ``job_id`` if its job is terminal, then drop
        it from the map. A no-op while the job still runs."""
        widget = self._detached_cards.get(job_id)
        if widget is None:
            return
        job = jobs.get(job_id)
        if job is None or job.status == "running":
            return
        report = job.result or ""
        if job.status in ("failed", "cancelled") or subagent_failed(report):
            status = "failed"
            # A PrerequisiteFailed report names the job that killed this
            # dependent; surface it on the header tag (the red ↳ line already
            # carries the full message).
            culprit = blocked_by_id(report)
            if culprit:
                widget.blocked_by = culprit
        else:
            status = "done"
        widget.finish(report, status=status)
        del self._detached_cards[job_id]

    def fill_finished_detached_cards(self, jobs) -> None:
        """Fill every mapped detached card whose job has settled. Called from the
        job-registry change hook so cards update live as background jobs complete."""
        # Waiting→running sweep: a settle may have unblocked an after=
        # dependent whose own job keeps running. set_waiting no-ops when
        # unchanged, so sweeping every card is cheap.
        for widget in self.subagents:
            if widget.waiting and not _deps_pending(widget.after_ids, jobs):
                widget.set_waiting(False)
        for job_id in list(self._detached_cards):
            self._fill_detached_card(job_id, jobs)
        # A settling background job changes a card's status/stats; repaint the
        # open screen so its list/summary tick live.
        self.app.subagents.refresh()

    def prune_completed(self) -> None:
        """Drop finished entries from ``tool_widgets`` at a turn boundary so the
        dict doesn't grow without bound across a long session.

        ``tool_widgets`` is only read mid-run: to look up a tool's widget when its
        result event arrives, to route a sub-agent's stream/notice events, and to
        skip the re-emitted call of a gated tool across approval rounds. All three
        concern *in-flight* calls — a widget whose status has left ``"pending"`` is
        done and will never be looked up again, so it's pure leak after the turn.
        We prune at the turn boundary (not per ``on_events`` / approval round) so
        the cross-round duplicate guard for gated tools still sees its entry while
        the turn is live.

        ``subagents`` is deliberately NOT pruned: it's the ordered backing list for
        the Ctrl+X viewer, which is meant to show every foreground sub-agent of the
        session — so its growth is intended, not a leak."""
        self.tool_widgets = {
            tid: w for tid, w in self.tool_widgets.items()
            if getattr(w, "status", None) == "pending"
        }
        for sid in list(self._sub_streams):
            if sid not in self.tool_widgets:
                del self._sub_streams[sid]

    def reset_live_tokens(self) -> None:
        self.live_run_tokens = 0

    def toggle_reveal_all(self) -> None:
        """Ctrl+O: reveal every tool output in full (open groups, uncap edit
        diffs, expand finished thoughts), or restore the default view on a second
        press."""
        self.show_all_output = not self.show_all_output
        for group in self.app.query(ToolGroupWidget):
            group.collapsed = not self.show_all_output
        for widget in self.app.query(ToolCallWidget):
            widget.set_reveal(self.show_all_output)
        for thought in self.app.query(ThinkingWidget):
            thought.set_reveal(self.show_all_output)

    def append_stream(self, widget: AssistantMessage | ThinkingWidget, delta: str) -> None:
        """Buffer a streamed delta into ``widget`` and mark it for the next flush
        tick. Funnelling every append through here is what lets the tick render
        only the streams that actually changed."""
        widget.append(delta)
        self.dirty_streams.add(widget)

    def flush_streams(self) -> None:
        """Render every AssistantMessage that buffered deltas since the last tick —
        top-level and nested sub-agent streams alike. Coalescing the markdown parses
        here is the streaming debounce; once the content overflows the log is
        anchored so it tail-follows. Draining the dirty set (rather than walking the
        whole message tree) keeps the tick proportional to the number of live
        streams."""
        dirty, self.dirty_streams = self.dirty_streams, set()
        for m in dirty:
            # Skip sub-agent transcripts that aren't on screen in the viewer:
            # re-parsing their full markdown every tick — ×N during a fan-out —
            # blocks the event loop and freezes the UI for no visible gain. Keep
            # them pending so they render the moment their card is viewed.
            if _stream_hidden(m, self.detail_host):
                self.dirty_streams.add(m)
                continue
            m.flush()
            # A stream that couldn't render this tick stays _pending — an
            # AssistantMessage holding off while a prior incremental append drains (so
            # it doesn't overlap Textual's parse cursor and double blocks), or a widget
            # not yet attached. Re-arm it so a later tick retries rather than stranding
            # the un-flushed tail until the next delta happens to re-add it.
            if getattr(m, "_pending", False):
                self.dirty_streams.add(m)
        self._drain_subagent_usage()
        self._anchor_on_overflow()
        # Coalesced sub-agents-screen repaint: streamed events mark the screen
        # dirty rather than repainting inline (a per-event DataTable rebuild + flush
        # pins a core during a fan-out); drain that here, once per frame, after the
        # visible pane's transcript has been flushed above.
        self.app.subagents.drain_repaint()
        # Piggyback on the same per-frame tick to repaint the status bar while a
        # turn is running, so the live token counter advances as the run streams.
        if self.app.status.busy:
            self.app.status.refresh_status()

    def note_subagent_usage(self, parent: SubAgentWidget, usage) -> None:
        """Stash a sub-agent's latest live usage to be priced on the next flush tick.

        Pricing (resolve_cost → a genai-prices table lookup) plus the token-split
        format is deferred and coalesced rather than run inline per event: a fast
        provider emits many text deltas between 80 ms ticks and a fan-out multiplies
        that ×N concurrent agents, so pricing per delta pins a core for a number that
        barely moves. The flush tick prices each card at most once per frame."""
        parent._pending_usage = usage
        self.dirty_usage_cards.add(parent)

    def _drain_subagent_usage(self) -> None:
        """Price the sub-agent cards that took new usage since the last frame (see
        note_subagent_usage). Coalesced to the flush tick and deduped on the token
        total, so a quiet card costs nothing and a streaming one is priced ≤12.5x/s,
        not once per delta."""
        if not self.dirty_usage_cards:
            return
        cards, self.dirty_usage_cards = self.dirty_usage_cards, set()
        for card in cards:
            usage = card._pending_usage
            if usage is None or usage.total_tokens == card._priced_tokens:
                continue
            card._priced_tokens = usage.total_tokens
            cost, _ = resolve_cost(usage, self.app.harness.model_id)
            cost_text = _format_cost(cost) if cost is not None else None
            card.set_usage(
                usage.total_tokens, cost_text, _format_token_split(usage),
                cost_value=cost,  # numeric cost for the summary roll-up
            )

    def _anchor_on_overflow(self) -> None:
        """Anchor the log the moment its content first overflows the viewport, so
        new content tail-follows and the intro header scrolls away with it. Until
        then the log stays top-aligned (header pinned at the top). We anchor exactly
        once (latched): afterwards Textual releases on a user scroll-up and
        re-engages at the bottom on its own. Gating on a transient ``is_anchored``
        instead would re-anchor — yanking the viewport down — on every flush tick
        while the user is scrolled up reading."""
        from textual.containers import VerticalScroll

        # Never anchor off a stale max_scroll_y mid-rebuild — render_session sets the
        # final anchor state itself once the new content is laid out.
        if self.rebuilding or self._anchored_on_overflow:
            return
        try:
            log = self.app.query_one("#log", VerticalScroll)
        except Exception:
            return
        if log.max_scroll_y > 0:
            log.anchor()
            self._anchored_on_overflow = True

    def _group_of(self, widget) -> ToolGroupWidget | None:
        """The ToolGroupWidget a tool widget lives in, if any (its body's parent)."""
        node = widget.parent
        while node is not None:
            if isinstance(node, ToolGroupWidget):
                return node
            node = node.parent
        return None

    async def add_tool_to_run(
        self,
        widget: ToolCallWidget,
        container,
        group: ToolGroupWidget | None,
        solo: ToolCallWidget | None,
    ) -> tuple[ToolGroupWidget | None, ToolCallWidget | None]:
        """Place a tool call into the current run of consecutive calls and return
        the updated (group, solo) run state. A lone call mounts bare — wrapping one
        tool in a group is pure overhead. The second call of a run promotes the
        pair into a group (reparenting the first, in place), and a burst then folds
        to one line. ``container`` is the mount target (the log, or a sub-agent
        body)."""
        if group is not None:
            await group.add_tool(widget)
            return group, None
        if solo is None:
            await container.mount(widget)
            return None, widget
        # Second call of the run: replace the lone widget with a group holding both,
        # keeping the group where the lone widget sat.
        group = ToolGroupWidget()
        await container.mount(group, after=solo)
        await solo.remove()
        await group.add_tool(solo)
        await group.add_tool(widget)
        return group, None

    def _with_wait_label(self, tool_name: str, args: dict) -> dict:
        """For a ``wait_for_job`` blocking on a sub-agent, return ``args`` with the
        sub-agent's label injected as ``_wait_label`` so the row reads
        "Wait · <task>" instead of a bare job id. ``args`` is returned unchanged for
        any other tool, or a wait on a non-sub-agent job."""
        if tool_name != "wait_for_job":
            return args
        label = _wait_subagent_label(args, self.app.harness.deps.jobs)
        return {**args, "_wait_label": label} if label else args

    def mount_spawn_widget(self, args: dict) -> SubAgentWidget:
        """Build the compact card for a foreground spawn_agent and register it in the
        ordered ``subagents`` list (the viewer's navigation backing). The transcript
        streams into the card's hidden body; the full view reveals it on demand, so
        a fan-out stays legible as a stack of cards with no inline expansion."""
        model_label = str(args.get("model") or self.app.harness.model_label or "")
        widget = SubAgentWidget(
            str(args.get("type", "")),
            str(args.get("task", "")),
            model_label,
            description=str(args.get("description") or ""),
        )
        self.subagents.append(widget)
        widget.after_ids = _after_ids(args)
        return widget

    def ensure_pane(self, widget: SubAgentWidget) -> "SubAgentPane | None":
        """Create (once) the detail-host pane for ``widget`` and attach it to the
        card. Returns the pane, or None if the host isn't mounted yet (headless /
        early calls) — callers tolerate None the way every UI callback does."""
        if self.detail_host is None:
            return None
        if widget.pane is not None:
            return widget.pane
        pane = self.detail_host.add_pane(
            widget.stream_id, widget.agent_type, widget.model_label,
            widget.display_title(), widget.agent_task,
        )
        # This pane is fed by the live stream, so its transcript is already on
        # screen — mark it loaded so the resume-time lazy-load never fires on it.
        # A still-running sub-agent has written no sidecar yet, so that load would
        # read nothing and wrongly append the "transcript unavailable for this
        # resumed sub-agent" note over the live content. Only panes rebuilt from a
        # persisted session (replay_history) stay unloaded so they lazy-load.
        pane.transcript_loaded = True
        widget.pane = pane
        return pane

    async def on_events(self, ctx, events) -> None:
        # Fresh run: clear any in-flight tally from a prior approval round so the
        # next round's usage replaces it rather than stacking (each agent.run gets
        # its own ctx.usage, cumulative for that run).
        self.live_run_tokens = 0
        # A new run starts a fresh run of consecutive tool calls.
        self.tool_group = None
        self.solo_tool = None
        sink = _TopLevelSink(self, self.app.query_one("#log", VerticalScroll))
        async for event in events:
            # ctx.usage carries the run's live running total (ctx is None in some
            # unit tests); fold it into the status counter via the flush tick.
            self.live_run_tokens = (
                getattr(getattr(ctx, "usage", None), "total_tokens", 0) or 0
            )
            await self.dispatch_stream_event(event, sink)
        # A round that ends on a thought (no following text/tool to trigger the
        # per-event cap) still collapses to its preview.
        trailing_thought = sink.get_thinking()
        if trailing_thought is not None:
            trailing_thought.finalize()
            sink.set_thinking(None)
        # Likewise a round ending on assistant text never saw a following event to
        # finalize it; do the clean stream-end reparse now (heals any duplication the
        # incremental render left behind — see AssistantMessage.finalize). The pointer
        # is left set as the turn's resting reply (finalize is idempotent).
        trailing_assistant = sink.get_assistant()
        if trailing_assistant is not None:
            trailing_assistant.finalize()

    async def on_subagent_event(self, stream_id: str, event, usage=None) -> None:
        """Route a spawned sub-agent's own stream into the SubAgentWidget that owns
        it. Shares dispatch_stream_event with the top-level handler, but through a
        sub-agent sink that mounts into the widget's pane (in the detail host) and
        tracks per-stream state. ``usage`` is the run's live RunUsage (or None): its
        total + cost ride on the breadcrumb card and the full cache split lands on
        the pane's usage line. Fired on the app's event loop, so direct widget
        mutation is safe and parallel streams stay race-free by stream_id."""
        parent = self.tool_widgets.get(stream_id)
        if not isinstance(parent, SubAgentWidget):
            return
        if usage is not None and usage.total_tokens:
            self.note_subagent_usage(parent, usage)
        await self.dispatch_stream_event(event, _SubAgentSink(self, parent, stream_id))
        self.app.subagents.refresh()  # list/summary tick live while open

    async def on_cli_activity(self, events: list) -> None:
        """Render a claude-cli model's own tool_use/tool_result as native tool cards
        in the MAIN transcript. That provider delegates the turn to ``claude -p`` and
        returns text only (Claude runs its own tools), so these display-only events
        arrive via this side-channel instead of pydantic_ai's stream — keeping them
        out of the agent graph (no double-execution). A ``_TopLevelSink`` shares the
        renderer's current-assistant/run state with ``on_events``, so a card mounted
        here finalizes the in-flight assistant text and the model's next text part
        opens a fresh message below it — preserving interleaving. Fired on the app's
        event loop during the live turn, so direct widget mutation is safe."""
        sink = _TopLevelSink(self, self.app.query_one("#log", VerticalScroll))
        for event in events:
            await self.dispatch_stream_event(event, sink)

    async def on_subagent_notice(self, stream_id: str, message: str) -> None:
        """Show an out-of-band status line (e.g. a transient-error retry) on the
        SubAgentWidget that owns ``stream_id``. A no-op if the card is gone. Fired on
        the app's event loop, so direct widget mutation is safe."""
        parent = self.tool_widgets.get(stream_id)
        if isinstance(parent, SubAgentWidget):
            parent.note_retry(message)

    async def on_subagent_usage(self, stream_id: str, usage) -> None:
        """Surface the final usage from a CLI spawn (which can only report tokens
        once, at the end) to its card and pane. Queued for the next flush tick —
        the pricer formats it identically to a live native sub-agent's usage."""
        parent = self.tool_widgets.get(stream_id)
        if isinstance(parent, SubAgentWidget) and usage is not None:
            self.note_subagent_usage(parent, usage)

    async def on_subagent_model(self, stream_id: str, model: str) -> None:
        """Relabel a spawn card with the real model the sub-agent reported (e.g. a
        claude-cli spawn's model from its stream), replacing the harness-model
        fallback chosen at card-creation time. Updates the card's stored label and,
        if its pane is already open, the pane's subtitle. No-op if the card is gone.
        Fired on the app's event loop, so direct widget mutation is safe."""
        parent = self.tool_widgets.get(stream_id)
        if isinstance(parent, SubAgentWidget):
            parent.set_model(model)
            if parent.pane is not None:
                parent.pane.set_model(model)

    async def _on_text_start(
        self, event: PartStartEvent, sink: "_StreamSink", container: Widget
    ) -> None:
        part = cast(TextPart, event.part)
        sink.set_run(None, None)  # assistant text ends the run of tools
        sink.on_text()  # live title status, useful while collapsed
        msg = AssistantMessage()
        sink.set_assistant(msg)
        await container.mount(msg)
        if part.content:
            self.append_stream(msg, part.content)

    async def _on_text_delta(self, event: PartDeltaEvent, sink: "_StreamSink") -> None:
        delta = cast(TextPartDelta, event.delta)
        msg = sink.get_assistant()
        if msg is not None:
            self.append_stream(msg, delta.content_delta or "")

    async def _on_thinking_start(
        self, event: PartStartEvent, sink: "_StreamSink", container: Widget
    ) -> None:
        # Reasoning streams as its own collapsed block, standalone like
        # assistant text (so it breaks any open tool run rather than nesting).
        part = cast(ThinkingPart, event.part)
        sink.set_run(None, None)
        widget = ThinkingWidget()
        sink.set_thinking(widget)
        await container.mount(widget)
        if part.content:
            self.append_stream(widget.body, part.content)

    async def _on_thinking_delta(self, event: PartDeltaEvent, sink: "_StreamSink") -> None:
        delta = cast(ThinkingPartDelta, event.delta)
        widget = sink.get_thinking()
        if widget is not None:
            self.append_stream(widget.body, delta.content_delta or "")

    async def _on_tool_call(
        self, event: FunctionToolCallEvent, sink: "_StreamSink", container: Widget
    ) -> None:
        # A gated tool re-emits its call event on the post-approval execution
        # pass; reuse the widget already mounted for this id rather than
        # mounting an orphaned duplicate.
        if event.part.tool_call_id in self.tool_widgets:
            return
        args = event.part.args_as_dict()
        if await sink.intercept_tool(event, args, container):
            return
        args = self._with_wait_label(event.part.tool_name, args)
        sink.on_tool(event.part.tool_name, args)  # live card status
        widget = ToolCallWidget(
            event.part.tool_name, args,
            workspace_root=self.app.harness.deps.workspace.root,
        )
        self.tool_widgets[event.part.tool_call_id] = widget
        group, solo = sink.get_run()
        group, solo = await self.add_tool_to_run(widget, container, group, solo)
        # Keep the run state in sync; a None value just means "no open group /
        # no lone call" for this stream.
        sink.set_run(group, solo)

    async def _on_tool_result(self, event: FunctionToolResultEvent, sink: "_StreamSink") -> None:
        widget = self.tool_widgets.get(event.tool_call_id)
        if widget is not None:
            content = str(getattr(event.part, "content", ""))
            if isinstance(widget, SubAgentWidget) and self.note_detached_spawn(
                content, widget, self.app.harness.deps.jobs
            ):
                pass  # detached: card stays pending, fills when its job settles
            else:
                status = status_from_part(event.part)
                # A spawn that failed returns its error as a normal (successful)
                # tool result, so detect the runner's failure text and mark the
                # card failed rather than letting it render a misleading ✓.
                if (
                    isinstance(widget, SubAgentWidget)
                    and status == "done"
                    and subagent_failed(content)
                ):
                    status = "failed"
                widget.finish(content, status=status)
                if isinstance(widget, SubAgentWidget):
                    # A finished card changes the screen's list/summary scalars.
                    self.app.subagents.refresh()
                if isinstance(widget, ToolCallWidget):
                    group = self._group_of(widget)
                    if group is not None:
                        # Read widget.status *after* finish() so a bash non-zero
                        # exit (self-flipped inside finish) is detected.
                        group.note_child_finished(failed=widget.status == "failed")
        sink.on_result(event)

    async def dispatch_stream_event(self, event, sink: "_StreamSink") -> None:
        """Route one streamed event to the right widget via ``sink``, which knows
        where to mount and how to read/write this stream's run-state. The top-level
        and sub-agent handlers differ only in that sink (and their own pre/post
        bookkeeping), so the six event branches live here once."""
        # If the sink has no container (e.g. a sub-agent sink whose pane isn't
        # mounted yet — headless mode or an early race), there's nowhere to mount
        # widgets; skip the event rather than crashing.
        if sink.container is None:
            return
        # Narrowed once here (non-None past the guard) and threaded into the
        # handlers that mount, so the base's ``container: Widget | None`` stays
        # honest without each handler re-checking.
        container: Widget = sink.container
        # A reasoning block is complete the moment any event other than its own
        # thinking-delta arrives — the next part has started, so cap the thought
        # to its preview now (Ctrl+O still reveals it). A thought that's still
        # streaming (more ThinkingPartDeltas to come) is left uncapped.
        if not (
            isinstance(event, PartDeltaEvent)
            and isinstance(event.delta, ThinkingPartDelta)
        ):
            active_thought = sink.get_thinking()
            if active_thought is not None:
                active_thought.finalize()
                sink.set_thinking(None)
        # Symmetrically, an assistant text block is complete once any event other
        # than its own text-delta arrives (a tool call, a thought, or the next text
        # part). Finalize it then so its incremental markdown is replaced by one clean
        # reparse, healing any blocks the streaming path doubled. We do NOT clear the
        # current-assistant pointer (finalize is latched/idempotent, so re-finalizing
        # on later events is a cheap no-op): callers read current_assistant as the
        # turn's resting reply, and _on_text_start overwrites it for the next part.
        if not (
            isinstance(event, PartDeltaEvent)
            and isinstance(event.delta, TextPartDelta)
        ):
            active_assistant = sink.get_assistant()
            if active_assistant is not None:
                active_assistant.finalize()
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            await self._on_text_start(event, sink, container)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, TextPartDelta):
            await self._on_text_delta(event, sink)
        elif isinstance(event, PartStartEvent) and isinstance(event.part, ThinkingPart):
            await self._on_thinking_start(event, sink, container)
        elif isinstance(event, PartDeltaEvent) and isinstance(event.delta, ThinkingPartDelta):
            await self._on_thinking_delta(event, sink)
        elif isinstance(event, FunctionToolCallEvent):
            await self._on_tool_call(event, sink, container)
        elif isinstance(event, FunctionToolResultEvent):
            await self._on_tool_result(event, sink)
