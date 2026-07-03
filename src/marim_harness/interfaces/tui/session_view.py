"""Session-view orchestration — extracted from HarnessApp.

Rebuilds the log when the active session changes (new / switch / clear), replays a
restored conversation, and notes auto-renames. Behavior only — it holds no state;
it reaches the app, status presenter, and stream renderer through ``self.app``."""

from textual.containers import Horizontal, VerticalScroll
from textual.widgets import Static

from ...compaction import summary_text
from ...runtime.harness import strip_turn_context
from .stream_render import status_from_part, subagent_failed
from .widgets import (
    AssistantMessage,
    NoticeMessage,
    SubAgentWidget,
    SummaryWidget,
    ThinkingWidget,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessage,
)
from .widgets.subagent_detail import SubAgentDetailHost


class SessionView:
    """Owns rebuilding/replaying the log for the active session. Constructed by the
    HarnessApp, which delegates new/switch/clear and the auto-rename callback here."""

    def __init__(self, app) -> None:
        self.app = app

    async def _replay_parts(
        self,
        part,
        container,
        mount_fn,
        tool_widgets: dict,
        group: ToolGroupWidget | None,
        solo: ToolCallWidget | None,
        parent_id: str | None = None,
    ) -> tuple[ToolGroupWidget | None, ToolCallWidget | None]:
        """Dispatch one message part to the appropriate widget.

        Handles the parts shared between main-log replay (replay_history) and
        sub-agent pane replay (replay_messages_into): TextPart, ThinkingPart,
        generic ToolCallPart, and ToolReturnPart.

        ``parent_id`` is the stream_id of the sub-agent card this replay is
        nested under (None for the top-level log), so a spawn replayed inside
        another spawn's pane tags its own card for the sub-agents screen's tree
        order — mirroring the live path's ``_claim_spawn``.

        Main-log-only arms (UserPromptPart, ask_user standalone mount, and
        SubAgentDetailHost pane creation with model_label fallback) are left
        to the caller.
        """
        from pydantic_ai.messages import TextPart, ThinkingPart, ToolCallPart, ToolReturnPart

        if isinstance(part, TextPart):
            if part.content:
                # Text output ends the current tool burst in both the main log and
                # sub-agent panes. Without this reset, a tool after text would be
                # incorrectly grouped with tools before it (original
                # replay_messages_into omitted this reset, which was a bug).
                group = None
                solo = None
                msg = AssistantMessage()
                await mount_fn(msg)
                self.app.stream.append_stream(msg, part.content)
        elif isinstance(part, ThinkingPart):
            if part.content:
                # Same reasoning as TextPart: thinking output breaks a tool run.
                group = None
                solo = None
                widget = ThinkingWidget()
                await mount_fn(widget)
                self.app.stream.append_stream(widget.body, part.content)
                # A replayed thought is already complete — cap it to its preview
                # so a resumed session matches the live resting state (Ctrl+O
                # still reveals the full text).
                widget.finalize()
        elif isinstance(part, ToolCallPart):
            args = part.args_as_dict()
            # Every spawn_agent call rebuilds as its SubAgentWidget card
            # (mirroring the live path) rather than a generic tool row —
            # foreground AND background, so the sub-agents screen repopulates
            # after a resume. The card also joins the renderer's backing list,
            # which the live path does in mount_spawn_widget; replay skipped it
            # historically, leaving the ctrl+x screen empty on a resumed session.
            if part.tool_name == "spawn_agent":
                group = None
                solo = None
                widget = SubAgentWidget(
                    str(args.get("type", "")),
                    str(args.get("task", "")),
                    str(args.get("model") or ""),
                    description=str(args.get("description") or ""),
                )
                widget.stream_id = part.tool_call_id
                widget.parent_id = parent_id
                if all(w.stream_id != widget.stream_id
                       for w in self.app.stream.subagents):
                    self.app.stream.subagents.append(widget)
                tool_widgets[part.tool_call_id] = widget
                await mount_fn(widget)
                # SubAgentDetailHost pane creation and model_label fallback are
                # main-log-only; replay_history handles them after this call returns.
            else:
                widget = ToolCallWidget(
                    part.tool_name, args,
                    workspace_root=self.app.harness.deps.workspace.root,
                )
                tool_widgets[part.tool_call_id] = widget
                group, solo = await self.app.stream.add_tool_to_run(
                    widget, container, group, solo,
                )
        elif isinstance(part, ToolReturnPart):
            widget = tool_widgets.get(part.tool_call_id)
            if widget is not None:
                content = str(part.content)
                if isinstance(widget, SubAgentWidget):
                    from .stream_render import _detached_job_id

                    job_id = _detached_job_id(content)
                    if job_id is not None:
                        # A detach handoff is a job-id receipt, not the report —
                        # finish_replayed_cards joins the real outcome from the
                        # persisted jobs history / sidecar meta after replay.
                        widget.detached = True
                        widget.job_id = job_id
                        return group, solo
                status = status_from_part(part)
                # A failed spawn returns its error as a normal tool result;
                # detect the runner's failure text so the card shows failed,
                # not a misleading ✓ (mirrors the live path).
                if (
                    isinstance(widget, SubAgentWidget)
                    and status == "done"
                    and subagent_failed(content)
                ):
                    status = "failed"
                widget.finish(content, status=status)
        return group, solo

    async def replay_history(self, log: VerticalScroll) -> None:
        """Re-render a restored conversation into the log so a resumed session
        looks like where you left off."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            ToolCallPart,
            UserPromptPart,
        )

        tool_widgets: dict[str, ToolCallWidget | SubAgentWidget] = {}
        # The current run of consecutive tool calls during replay, mirroring the
        # live path so a resumed session groups bursts the same way: a lone call
        # stays bare, a burst folds into a group.
        group: ToolGroupWidget | None = None
        solo: ToolCallWidget | None = None
        for message in self.app.harness.session.history:
            if isinstance(message, (ModelRequest, ModelResponse)):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        group = None
                        solo = None
                        content = part.content
                        if isinstance(content, str):
                            text = content
                        elif isinstance(content, list):
                            text = " ".join(
                                item for item in content
                                if isinstance(item, str)
                            )
                        else:
                            text = str(content)
                        # A compaction summary renders as its own collapsed block,
                        # not as a user message it would otherwise be mistaken for.
                        body = summary_text(text)
                        if body is not None:
                            await log.mount(SummaryWidget(body))
                            continue
                        # Drop any turn-context envelope (job digests, hook
                        # output, error notes) so the log shows only what the
                        # user typed — as the live path already does.
                        await log.mount(UserMessage(strip_turn_context(text)))
                    elif isinstance(part, ToolCallPart) and part.tool_name == "ask_user":
                        # Mirror the live path (intercept_tool): ask_user mounts
                        # standalone and breaks the run, so the question + answer
                        # aren't buried in a collapsed tool group on a resumed
                        # session.
                        group = None
                        solo = None
                        args = part.args_as_dict()
                        widget = ToolCallWidget(
                            part.tool_name, args,
                            workspace_root=self.app.harness.deps.workspace.root,
                        )
                        tool_widgets[part.tool_call_id] = widget
                        await log.mount(widget)
                    else:
                        group, solo = await self._replay_parts(
                            part, log, log.mount, tool_widgets, group, solo,
                        )
                        # Main-log-only post-processing: every replayed spawn_agent
                        # (foreground and background alike) needs a
                        # SubAgentDetailHost pane for lazy transcript load on
                        # resume, and falls back to harness.model_label when the
                        # spawn didn't specify a model explicitly.
                        if (
                            isinstance(part, ToolCallPart)
                            and part.tool_name == "spawn_agent"
                        ):
                            args = part.args_as_dict()
                            model_label = str(
                                args.get("model") or self.app.harness.model_label or ""
                            )
                            widget = tool_widgets.get(part.tool_call_id)
                            if isinstance(widget, SubAgentWidget):
                                widget.model_label = model_label
                                host = self.app.query_one(SubAgentDetailHost)
                                # Mirror the live path (ensure_pane): the derived
                                # one-line title on the headline, the full task in
                                # the "▸ task" disclosure — not the title in both.
                                pane = host.add_pane(
                                    part.tool_call_id,
                                    str(args.get("type", "")),
                                    model_label,
                                    widget.display_title(),
                                    widget.agent_task,
                                )
                                widget.pane = pane

    async def replay_messages_into(
        self, pane, messages, parent_id: str | None = None,
    ) -> None:
        """Render resumed sub-agent transcript messages into ``pane``.

        Drives the same per-part widget construction as ``replay_history`` but
        targets a ``SubAgentPane`` (VerticalScroll) instead of the main log.
        ``parent_id`` is the owning card's stream_id, so a nested spawn found
        inside this transcript tags its own card for the sub-agents screen's
        tree order. Sets ``pane.transcript_loaded = True`` when done."""
        from pydantic_ai.messages import ModelRequest, ModelResponse

        tool_widgets: dict = {}
        group: ToolGroupWidget | None = None
        solo: ToolCallWidget | None = None
        for message in messages:
            if isinstance(message, (ModelRequest, ModelResponse)):
                for part in message.parts:
                    group, solo = await self._replay_parts(
                        part, pane, pane.add, tool_widgets, group, solo,
                        parent_id=parent_id,
                    )
        self.app.stream.flush_streams()
        pane.transcript_loaded = True

    _REPAIR_STUB_MARKER = "interrupted before completion"

    async def finish_replayed_cards(self) -> None:
        """Settle every replayed card's final state from the persisted record:
        the jobs history supplies a background spawn's status/report (its
        ToolReturnPart is only a job-id handoff), and the sidecar meta scan flags
        spawns that died mid-run as interrupted — including ones whose owning
        turn never persisted, which get a card synthesized from meta alone so no
        work silently vanishes."""
        store = self.app.harness.session.store
        if store is None:
            return
        from ...session import TranscriptStore

        metas = TranscriptStore(store.path, store.session_id).scan_meta()
        jobs = self.app.harness.deps.jobs
        settled = {j.stream_id: j for j in jobs.history if j.stream_id}
        for card in list(self.app.stream.subagents):
            job = settled.get(card.stream_id)
            meta = metas.get(card.stream_id)
            meta_status = meta.get("status") if meta else None
            if card.status == "pending":
                # A detached card whose handoff we skipped in _replay_parts.
                if job is not None:
                    status = "failed" if job.status in ("failed", "cancelled") else "done"
                    card.finish(job.result or "", status=status)
                elif meta_status == "finished":
                    card.finish("", status="done")
                elif meta_status == "failed":
                    card.finish("", status="failed")
                elif meta_status == "running":
                    # A sidecar checkpointed mid-run but never finalized: the spawn
                    # was cut down while working. It has a resumable transcript, so
                    # surface it as interrupted (▸ press r on the ctrl+x screen).
                    card.finish("", status="interrupted")
                else:
                    # No settled job AND no sidecar meta: the spawn_agent call never
                    # actually ran (e.g. Pydantic arg-validation rejected it, leaving
                    # a RetryPromptPart and no ToolReturnPart/sidecar). There is
                    # nothing to resume — resume_spawn refuses a card with no meta —
                    # so finish it "failed" rather than a forever-pending
                    # "interrupted" ghost that dangles a dead press-r affordance.
                    card.finish(
                        "spawn never ran (no transcript recorded)", status="failed"
                    )
            elif (meta_status == "running" and job is None
                  and self._REPAIR_STUB_MARKER in card.report):
                # A foreground spawn cut down mid-run: the main history's repair
                # stub finished the card "done", but the sidecar (whose final
                # write never happened) knows it never completed.
                card.finish(card.report, status="interrupted")
        # Spawns with a running sidecar but no card at all: the owning turn was
        # never persisted (crash before the turn's persist). Synthesize a card
        # from meta so the work is discoverable and resumable.
        have = {w.stream_id for w in self.app.stream.subagents}
        log = self.app.query_one("#log", VerticalScroll)
        for sid, meta in metas.items():
            if meta.get("status") != "running" or sid in have or sid in settled:
                continue
            widget = SubAgentWidget(
                str(meta.get("type", "")), str(meta.get("task", "")),
                str(meta.get("model") or self.app.harness.model_label or ""),
            )
            widget.stream_id = sid
            self.app.stream.subagents.append(widget)
            await log.mount(widget)
            host = self.app.query_one(SubAgentDetailHost)
            pane = host.add_pane(sid, widget.agent_type, widget.model_label,
                                 widget.display_title(), widget.agent_task)
            widget.pane = pane  # transcript_loaded stays False → lazy sidecar load
            widget.finish("", status="interrupted")

    async def replay_and_settle(self, log: VerticalScroll) -> None:
        """Replay the restored history into ``log`` and then settle every replayed
        card from the persisted record. This is the single seam both rebuild entry
        points route through — the session-switch/clear path (``render_session``)
        AND the startup ``marim --resume`` path (``app.on_mount``) — so a resumed
        session settles its sub-agent cards identically no matter how it was opened.

        Keeping the two on one seam is the load-bearing invariant: when the settle
        step lived only in ``render_session``, a normal startup resume replayed the
        history but never settled it, so a spawn killed mid-run left no interrupted
        card and was unresumable on the feature's main path.

        ``finish_replayed_cards`` must run even with NO history: a crash can leave a
        sidecar checkpointed mid-run with its owning turn never persisted, and the
        synthesized-card branch is the only thing that surfaces that work."""
        if self.app.harness.session.history:
            await self.replay_history(log)
        await self.finish_replayed_cards()

    async def mount_header(self, log: VerticalScroll) -> AssistantMessage:
        """Mount the two-column intro header — the MARIM banner on the left, the
        welcome/resume text on the right (Claude-style) — and return the intro
        AssistantMessage for the caller to stream into."""
        from .app import _BANNER

        intro = AssistantMessage()
        await log.mount(
            Horizontal(
                Static(_BANNER, id="banner", markup=False),
                intro,
                id="intro-header",
            )
        )
        return intro

    def on_rename(self, old: str, new: str) -> None:
        """Note an automatic session title in the log. Called synchronously from
        the background autoname task (on the app's event loop); mount without
        awaiting."""
        log = self.app.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"session renamed: {new}"))
        self.app.status.refresh_title()  # the new name shows in the terminal title
        self.app.status.refresh_status()

    async def render_session(self, note: str) -> None:
        """Rebuild the log for a fresh view of the active session: banner, an
        intro note, then a replay of any restored history."""
        self.app.stream.reset()
        log = self.app.query_one("#log", VerticalScroll)
        # Guard the rebuild: while old content is torn down and new content mounted,
        # the log's max_scroll_y is stale, so an interval flush tick must not anchor
        # off it (that left a cleared session bottom-aligned). We set the final
        # anchor state ourselves once the new content is laid out.
        self.app.stream.rebuilding = True
        try:
            log.anchor(False)  # drop any anchor inherited from the previous session
            await log.remove_children()
            # The detail host's transcript panes are per-session live state; a
            # rebuild for a different (or reloaded) session must start from an empty
            # host, or replay re-adds a pane with the same deterministic pane_id and
            # hard-crashes with DuplicateIds. Clearing here is safe against the live
            # streaming path (StreamRenderer.ensure_pane): render_session never runs
            # mid-turn, so no stream is feeding a pane while this tears them down.
            await self.app.query_one(SubAgentDetailHost).clear_panes()
            intro = await self.mount_header(log)
            self.app.stream.append_stream(intro, note)
            await self.replay_and_settle(log)
            self.app.stream.flush_streams()  # render the rebuilt log before first paint
        finally:
            self.app.stream.rebuilding = False
        # A restored session opens at the bottom; a fresh/cleared one stays top-
        # aligned (header pinned) until a turn's output overflows the viewport.
        restored = bool(self.app.harness.session.history)
        log.anchor(restored)
        # Seed the overflow latch to match: a restored view is already anchored, so
        # a later flush must not re-anchor; a fresh/cleared one anchors on its first
        # overflow.
        self.app.stream._anchored_on_overflow = restored
        self.app.status.refresh_title()  # reflect the switched-to session's name
        self.app.status.refresh_status()
        self.app._render_tasks()
        self.app._render_jobs()  # jobs are process-scoped, not per-session

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd)."""
        from .app import _WELCOME

        self.app.harness.reset()
        await self.app.harness.session_start("clear")
        await self.render_session(_WELCOME)

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk."""
        self.app.harness.new_session(name)
        await self.app.harness.session_start("startup")
        label = self.app.harness.session.session_name or "new session"
        await self.render_session(f"**New session** — `{label}`.")

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off."""
        n = self.app.harness.switch_session(session_id)
        await self.app.harness.session_start("resume")
        label = self.app.harness.session.session_name or session_id
        await self.render_session(
            f"**Switched to** `{label}` — {n} messages restored."
        )
