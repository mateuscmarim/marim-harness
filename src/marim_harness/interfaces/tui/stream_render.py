"""The event→widget streaming engine — extracted from HarnessApp.

Turns a turn's (and each sub-agent's) streamed events into the log's live
AssistantMessage / ToolCallWidget / SubAgentWidget tree. Owns all per-turn stream
state; reaches the app and the status presenter through ``self.app``."""

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
from .widgets import (
    AssistantMessage,
    SubAgentWidget,
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
_SUBAGENT_FAIL_PREFIXES = (
    "No sub-agent type ",
    "Can't run sub-agent ",
    "Failed to build sub-agent",
    "Isolated spawn needs ",
    "Couldn't create an isolated worktree",
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


def _detached_job_id(content: str) -> str | None:
    """The job id from an auto-detached spawn's handoff note (the text
    ``_detach_handoff`` returns), or None for any other tool return. Paired with
    that producer — a round-trip test pins the two formats together."""
    text = content.lstrip()
    if not text.startswith(_DETACH_PREFIX):
        return None
    job_id, sep, _ = text[len(_DETACH_PREFIX):].partition(",")
    return job_id.strip() if sep and job_id.strip() else None


def _wait_card_spec(tool_name: str, args: dict, jobs) -> tuple[str, str] | None:
    """If a ``wait_for_job`` call targets an *already-finished* agent job, return
    ``(agent_type, agent_task)`` so the wait renders as that sub-agent's card —
    the moment a detached spawn's result becomes available inline. ``None`` when
    it isn't such a call, notably when the job is still running: the wait may then
    time out with no report, so a card (which has no honest "still waiting" state)
    would be misleading; the plain tool row carries the timeout text instead.
    Reads ``type``/``task`` back out of the job label (``f"{type}: {task}"``)."""
    if tool_name != "wait_for_job":
        return None
    job = jobs.get(str(args.get("id", "")))
    if job is None or job.kind != "agent" or job.status == "running":
        return None
    agent_type, _, agent_task = job.label.partition(": ")
    return agent_type, agent_task


def _stream_hidden(widget: Widget, viewing_sid: str | None) -> bool:
    """True when ``widget`` is a sub-agent transcript stream that isn't currently
    being viewed, so re-parsing its markdown every flush tick would be wasted work
    (and, ×N during a fan-out, would freeze the UI). A stream owned by a
    ``SubAgentWidget`` is hidden unless that card's ``stream_id`` is the one on
    screen in the viewer; a top-level log stream (no such ancestor) is never
    hidden."""
    node = widget.parent
    while node is not None:
        if isinstance(node, SubAgentWidget):
            return node.stream_id != viewing_sid
        node = node.parent
    return False


class _StreamSink:
    """Where one event stream's widgets land and how its run-state is read/written.

    Routing a streamed turn is identical whether the events come from the
    top-level agent or a nested sub-agent — the only things that differ are the
    mount container, where this stream's run-state and current assistant message
    live, the title bookkeeping, and whether a tool call gets intercepted (the
    spawn_agent special case). A sink captures exactly those, so one dispatch core
    (:meth:`StreamRenderer.dispatch_stream_event`) serves both. Hooks default to
    no-ops; sub-classes override only what their scope needs."""

    container: Widget  # mount target for assistant text and bare tool widgets

    def get_run(self) -> tuple:
        """This stream's (group, solo) run-of-consecutive-tools state."""
        raise NotImplementedError

    def set_run(self, group, solo) -> None:
        raise NotImplementedError

    def get_assistant(self):
        """The AssistantMessage currently receiving text deltas, or None."""
        raise NotImplementedError

    def set_assistant(self, msg) -> None:
        raise NotImplementedError

    def get_thinking(self):
        """The ThinkingWidget currently receiving thinking deltas, or None."""
        raise NotImplementedError

    def set_thinking(self, widget) -> None:
        raise NotImplementedError

    def on_text(self) -> None:
        """Called when the stream starts a text part (title status, sub only)."""

    def on_tool(self, tool_name: str, args: dict) -> None:
        """Called when the stream makes a tool call (card status, sub only)."""

    async def intercept_tool(self, event, args: dict) -> bool:
        """Give the scope first refusal on a tool call; return True to claim it and
        skip the default ToolCallWidget path. Default: never intercepts."""
        return False

    def on_result(self, event) -> None:
        """Called after a tool result is rendered (cleanup hook)."""


class _TopLevelSink(_StreamSink):
    """The top-level turn stream: mounts into the main log, keeps run-state and the
    current assistant on the renderer's scalar fields, and claims foreground
    spawn_agent calls so they render as a live SubAgentWidget instead of a generic
    tool."""

    def __init__(self, renderer: "StreamRenderer", container) -> None:
        self._r = renderer
        self.container = container

    def get_run(self) -> tuple:
        return self._r.tool_group, self._r.solo_tool

    def set_run(self, group, solo) -> None:
        self._r.tool_group = group
        self._r.solo_tool = solo

    def get_assistant(self):
        return self._r.current_assistant

    def set_assistant(self, msg) -> None:
        self._r.current_assistant = msg

    def get_thinking(self):
        return self._r.current_thinking

    def set_thinking(self, widget) -> None:
        self._r.current_thinking = widget

    async def intercept_tool(self, event, args: dict) -> bool:
        # A background spawn returns a job id immediately and doesn't stream its
        # steps, so it falls through to a plain tool widget; only a foreground
        # spawn gets the live SubAgentWidget (and is mounted standalone, breaking
        # the run so it isn't buried in a tool group).
        if event.part.tool_name == "spawn_agent" and not args.get("background"):
            widget = self._r.mount_spawn_widget(args)
            widget.stream_id = event.part.tool_call_id
            self._r.tool_widgets[event.part.tool_call_id] = widget
            self.set_run(None, None)
            await self.container.mount(widget)
            return True
        # ask_user is a user-facing Q&A, not mechanical work — keep it out of the
        # collapsed tool group, where the question and the user's answer would be
        # hidden behind a "≡ N tools" fold. Render a normal tool widget but mount
        # it standalone and break the run on both sides (same rationale as the
        # foreground spawn_agent case above).
        if event.part.tool_name == "ask_user":
            widget = ToolCallWidget(
                event.part.tool_name, args,
                workspace_root=self._r.app.harness.deps.workspace_root,
            )
            self._r.tool_widgets[event.part.tool_call_id] = widget
            self.set_run(None, None)
            await self.container.mount(widget)
            return True
        # Waiting on an already-finished detached sub-agent: render its card
        # (filled by the result handler's finish()) instead of a plain tool row.
        spec = _wait_card_spec(
            event.part.tool_name, args, self._r.app.harness.deps.jobs
        )
        if spec is not None:
            agent_type, agent_task = spec
            widget = SubAgentWidget(
                agent_type, agent_task, self._r.app.harness.model_label or ""
            )
            widget.stream_id = event.part.tool_call_id
            self._r.tool_widgets[event.part.tool_call_id] = widget
            self.set_run(None, None)
            await self.container.mount(widget)
            return True
        return False

    def on_result(self, event) -> None:
        # A foreground spawn's stream_id is its tool_call_id; drop its sub-agent
        # assistant entry once the spawn returns.
        self._r.sub_assistants.pop(event.tool_call_id, None)


class _SubAgentSink(_StreamSink):
    """A nested sub-agent stream: mounts into its SubAgentWidget body, keeps
    run-state and the current assistant in per-stream dicts keyed by ``stream_id``,
    and pushes live text/tool activity into the (collapsed) widget title."""

    def __init__(self, renderer: "StreamRenderer", parent: SubAgentWidget,
                 stream_id: str) -> None:
        self._r = renderer
        self._parent = parent
        self._sid = stream_id
        self.container = parent.body

    def get_run(self) -> tuple:
        return (self._r.sub_tool_groups.get(self._sid),
                self._r.sub_solo_tools.get(self._sid))

    def set_run(self, group, solo) -> None:
        self._r.sub_tool_groups[self._sid] = group
        self._r.sub_solo_tools[self._sid] = solo

    def get_assistant(self):
        return self._r.sub_assistants.get(self._sid)

    def set_assistant(self, msg) -> None:
        self._r.sub_assistants[self._sid] = msg

    def get_thinking(self):
        return self._r.sub_thinkings.get(self._sid)

    def set_thinking(self, widget) -> None:
        self._r.sub_thinkings[self._sid] = widget

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
        self.sub_tool_groups: dict[str, ToolGroupWidget | None] = {}
        self.sub_solo_tools: dict[str, ToolCallWidget | None] = {}
        self.sub_assistants: dict[str, AssistantMessage] = {}
        self.sub_thinkings: dict[str, ThinkingWidget] = {}
        # Every foreground sub-agent spawned this session, in spawn order — the
        # backing list for the full-screen viewer's navigation and "(i of N)"
        # count. ``viewing_sid`` is the stream_id of the card currently shown in
        # the viewer (None when the viewer is closed); the flush tick uses it to
        # render only the on-screen transcript.
        self.subagents: list[SubAgentWidget] = []
        # job_id → a pending detached-spawn card, awaiting its background job's
        # report. Filled on job settle (fill_finished_detached_cards); cleared on
        # session reset. Not pruned per-turn: the job finishes after the turn ends.
        self._detached_cards: dict[str, SubAgentWidget] = {}
        self.viewing_sid: str | None = None
        # Either an AssistantMessage (reply/sub-agent body) or a ThinkingWidget —
        # both expose the append/flush streaming interface the tick drains.
        self.dirty_streams: set[AssistantMessage | ThinkingWidget] = set()
        self.live_run_tokens = 0
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
        self.sub_tool_groups.clear()
        self.sub_solo_tools.clear()
        self.sub_assistants.clear()
        self.sub_thinkings.clear()
        self.subagents.clear()
        self._detached_cards.clear()
        self.viewing_sid = None
        self.dirty_streams.clear()

    def note_detached_spawn(self, content: str, widget: "SubAgentWidget", jobs) -> bool:
        """If ``content`` is a detached-spawn handoff, keep ``widget`` pending and
        map its job_id → card so it fills when the job settles; return True so the
        caller does NOT finish the card. Fills at once if the job already settled
        (a fast job can finish before its handoff renders). Returns False for a
        normal report, so foreground spawns and wait_for_job cards finish as usual."""
        job_id = _detached_job_id(content)
        if job_id is None:
            return False
        widget.activity = "running in background…"
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
        else:
            status = "done"
        widget.finish(report, status=status)
        del self._detached_cards[job_id]

    def fill_finished_detached_cards(self, jobs) -> None:
        """Fill every mapped detached card whose job has settled. Called from the
        job-registry change hook so cards update live as background jobs complete."""
        for job_id in list(self._detached_cards):
            self._fill_detached_card(job_id, jobs)

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

    def reset_live_tokens(self) -> None:
        self.live_run_tokens = 0

    def toggle_reveal_all(self) -> None:
        """Ctrl+O: reveal every tool output in full (open groups, uncap edit
        diffs), or restore the default view on a second press."""
        self.show_all_output = not self.show_all_output
        for group in self.app.query(ToolGroupWidget):
            group.collapsed = not self.show_all_output
        for widget in self.app.query(ToolCallWidget):
            widget.set_reveal(self.show_all_output)

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
            if _stream_hidden(m, self.viewing_sid):
                self.dirty_streams.add(m)
                continue
            m.flush()
        self._anchor_on_overflow()
        # Piggyback on the same per-frame tick to repaint the status bar while a
        # turn is running, so the live token counter advances as the run streams.
        if self.app.status.busy:
            self.app.status.refresh_status()

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

    def mount_spawn_widget(self, args: dict):
        """Build the compact card for a foreground spawn_agent and register it in the
        ordered ``subagents`` list (the viewer's navigation backing). The transcript
        streams into the card's hidden body; the full view reveals it on demand, so
        a fan-out stays legible as a stack of cards with no inline expansion."""
        model_label = str(args.get("model") or self.app.harness.model_label or "")
        widget = SubAgentWidget(
            str(args.get("type", "")),
            str(args.get("description") or args.get("task", "")),
            model_label,
        )
        self.subagents.append(widget)
        return widget

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

    async def on_subagent_event(self, stream_id: str, event, usage=None) -> None:
        """Route a spawned sub-agent's own stream into the SubAgentWidget that owns
        it. Shares dispatch_stream_event with the top-level handler, but through a
        sub-agent sink that mounts into the widget body and tracks per-stream state.
        ``usage`` is the run's live RunUsage (or None): its total + cost ride in the
        (collapsed) title and the full cache split lands in the expanded body. Fired
        on the app's event loop, so direct widget mutation is safe and parallel
        streams stay race-free by stream_id."""
        parent = self.tool_widgets.get(stream_id)
        if not isinstance(parent, SubAgentWidget):
            return
        if usage is not None and usage.total_tokens:
            cost, _ = resolve_cost(usage, self.app.harness.model_id)
            cost_text = _format_cost(cost) if cost is not None else None
            parent.set_usage(usage.total_tokens, cost_text, _format_token_split(usage))
        await self.dispatch_stream_event(event, _SubAgentSink(self, parent, stream_id))

    async def on_subagent_notice(self, stream_id: str, message: str) -> None:
        """Show an out-of-band status line (e.g. a transient-error retry) on the
        SubAgentWidget that owns ``stream_id``. A no-op if the card is gone. Fired on
        the app's event loop, so direct widget mutation is safe."""
        parent = self.tool_widgets.get(stream_id)
        if isinstance(parent, SubAgentWidget):
            parent.note_retry(message)

    async def dispatch_stream_event(self, event, sink: _StreamSink) -> None:
        """Route one streamed event to the right widget via ``sink``, which knows
        where to mount and how to read/write this stream's run-state. The top-level
        and sub-agent handlers differ only in that sink (and their own pre/post
        bookkeeping), so the four event branches live here once."""
        if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
            sink.set_run(None, None)  # assistant text ends the run of tools
            sink.on_text()  # live title status, useful while collapsed
            msg = AssistantMessage()
            sink.set_assistant(msg)
            await sink.container.mount(msg)
            if event.part.content:
                self.append_stream(msg, event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta, TextPartDelta
        ):
            msg = sink.get_assistant()
            if msg is not None:
                self.append_stream(msg, event.delta.content_delta or "")
        elif isinstance(event, PartStartEvent) and isinstance(
            event.part, ThinkingPart
        ):
            # Reasoning streams as its own collapsed block, standalone like
            # assistant text (so it breaks any open tool run rather than nesting).
            sink.set_run(None, None)
            widget = ThinkingWidget()
            sink.set_thinking(widget)
            await sink.container.mount(widget)
            if event.part.content:
                self.append_stream(widget.body, event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta, ThinkingPartDelta
        ):
            widget = sink.get_thinking()
            if widget is not None:
                self.append_stream(widget.body, event.delta.content_delta or "")
        elif isinstance(event, FunctionToolCallEvent):
            # A gated tool re-emits its call event on the post-approval execution
            # pass; reuse the widget already mounted for this id rather than
            # mounting an orphaned duplicate.
            if event.part.tool_call_id in self.tool_widgets:
                return
            args = event.part.args_as_dict()
            if await sink.intercept_tool(event, args):
                return
            sink.on_tool(event.part.tool_name, args)  # live card status
            widget = ToolCallWidget(
                event.part.tool_name, args,
                workspace_root=self.app.harness.deps.workspace_root,
            )
            self.tool_widgets[event.part.tool_call_id] = widget
            group, solo = sink.get_run()
            group, solo = await self.add_tool_to_run(
                widget, sink.container, group, solo
            )
            # Keep the run state in sync; a None value just means "no open group /
            # no lone call" for this stream.
            sink.set_run(group, solo)
        elif isinstance(event, FunctionToolResultEvent):
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
                    if isinstance(widget, ToolCallWidget):
                        group = self._group_of(widget)
                        if group is not None:
                            # Read widget.status *after* finish() so a bash non-zero
                            # exit (self-flipped inside finish) is detected.
                            group.note_child_finished(failed=widget.status == "failed")
            sink.on_result(event)
