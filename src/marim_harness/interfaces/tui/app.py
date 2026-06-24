import time
from asyncio import CancelledError

from pydantic_ai import ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Static

from ...agent import Harness
from ...errors import format_provider_error
from ...history import PromptHistory
from ...prefs import load_theme, save_theme
from ...usage import resolve_cost
from .approval import ApprovalModal
from .ask_user import AskUserModal
from .commands import dispatch
from .model_picker import ModelPickerModal
from .queue import QueuedMessage
from .session_view import SessionView
from .settings import SettingsModal
from .status import (
    _CLOCK_TICK_INTERVAL,
    _SPINNER_TICK_INTERVAL,
    StatusPresenter,
    format_duration,
    osc_title,
)
from .stream_render import StreamRenderer
from .themes import MARIM_THEMES
from .wake import WakeController
from .widgets import (
    AssistantMessage,
    CommandAutocomplete,
    ErrorMessage,
    JobPanel,
    NoticeMessage,
    PromptInput,
    QueuePanel,
    SubAgentFooter,
    SubAgentList,
    SummaryWidget,
    TaskPanel,
    TurnMeta,
    UserMessage,
    format_cost,
    human_tokens,
)

_BANNER = (
    " ███╗   ███╗ █████╗ ██████╗ ██╗███╗   ███╗\n"
    " ████╗ ████║██╔══██╗██╔══██╗██║████╗ ████║\n"
    " ██╔████╔██║███████║██████╔╝██║██╔████╔██║\n"
    " ██║╚██╔╝██║██╔══██║██╔══██╗██║██║╚██╔╝██║\n"
    " ██║ ╚═╝ ██║██║  ██║██║  ██║██║██║ ╚═╝ ██║\n"
    " ╚═╝     ╚═╝╚═╝  ╚═╝╚═╝  ╚═╝╚═╝╚═╝     ╚═╝\n"
    "   · · ·   a   t e r m i n a l   h a r n e s s"
)

# How often (seconds) buffered streaming text is rendered. ~12 flushes/sec reads
# as smooth while collapsing many per-token markdown re-parses into one.
_STREAM_FLUSH_INTERVAL = 0.08

_WELCOME = (
    "Type a message below to start, or `/help` for commands.\n\n"
    "- `/` opens the command menu — `↑`/`↓` to move, `tab` to complete\n"
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `ctrl+g` (or `alt+enter`) steers the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits"
)


class HarnessApp(App):
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("ctrl+o", "toggle_outputs", "Show all output"),
        ("ctrl+x", "toggle_subagents", "Subagents"),
        ("escape", "cancel_turn", "Cancel turn"),
        ("ctrl+r", "run_queued", "Run queued"),
        ("ctrl+c", "quit", "Quit"),
    ]

    def __init__(self, harness: Harness, history: PromptHistory | None = None) -> None:
        super().__init__()
        self.harness = harness
        self.status = StatusPresenter(self)
        self.stream = StreamRenderer(self)
        self.session = SessionView(self)
        # Recallable prompt history. Defaults to in-memory; the CLI passes a
        # persistent one so Up/Down recall prompts across restarts.
        self._history = history if history is not None else PromptHistory()
        self.harness.bind_ui(
            request_approval=self._request_approval,
            ask_user=self._ask_user,
            on_subagent_event=self.stream.on_subagent_event,
            on_subagent_notice=self.stream.on_subagent_notice,
            on_tasks_changed=self._on_tasks_changed,
            on_jobs_changed=self._on_jobs_changed,
            on_compact=self._on_compact,
            on_compact_start=self._on_compact_start,
            on_rename=self.session.on_rename,
        )
        self._compacting_notice: NoticeMessage | None = None
        self._vision_caps: dict[str, bool | None] = {}
        self._turn_worker = None
        # Latch closing the window between "decided to start a turn" and the
        # exclusive worker actually existing. _start_turn awaits a mount before it
        # can set _turn_worker, so without this a second submit landing in that gap
        # would pass the _turn_worker guard and start a *duplicate* exclusive
        # worker — which Textual resolves by silently cancelling the first, the
        # exact hazard turn_busy exists to prevent. Set before the first await,
        # cleared on every _start_turn exit path.
        self._turn_starting = False
        self._queue: list[QueuedMessage] = []
        self._queue_paused = False
        self._queue_seq = 0
        # Confirm-once quit latch: set True by the first quit attempt that warns
        # about pending queued messages. One-way for the process — once the user
        # has been warned, later quits proceed without re-warning.
        self._quit_armed = False
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off`.
        self.autonomous_wake = harness.autonomous_wake
        # Bounds the wake→spawn→wake chain and owns the should-wake decision; the
        # App keeps the public autonomous_wake toggle and the wake's side effects.
        self._wake = WakeController(harness.wake_depth_cap)
        # Ids of finished (done/failed) jobs already desktop-notified, so each
        # completion pings exactly once, independent of the autonomous-wake path.
        self._notified_jobs: set[str] = set()
        self._autocomplete: CommandAutocomplete | None = None
        # Full-screen sub-agent viewer state: whether it's open and which spawned
        # sub-agent (index into stream.subagents) is on screen.
        self.subagent_viewer_open = False
        self.subagent_index = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield QueuePanel()
        yield Static(self.status.status_text(), id="status-bar")
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput(history=self._history)
        # The full-screen sub-agent viewer chrome (hidden until ctrl+x). The
        # transcript itself is revealed in place on each SubAgentWidget.body; these
        # two are the side-panel list and the status footer that frame it.
        yield SubAgentList()
        yield SubAgentFooter(id="subagent-footer")
        yield Footer()

    async def on_mount(self) -> None:
        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.theme = load_theme()
        self.sub_title = str(self.harness.deps.workspace_root)
        self.status.refresh_title()
        log = self.query_one("#log", VerticalScroll)
        intro = await self.session.mount_header(log)
        if self.harness.session.history:
            n = len(self.harness.session.history)
            tokens = self.harness.session.total_tokens
            self.stream.append_stream(
                intro,
                f"**Resumed session** — {n} messages, {tokens} tokens restored.",
            )
            await self.session.replay_history(log)
        else:
            self.stream.append_stream(intro, _WELCOME)
        self.stream.flush_streams()  # render the static intro/replay before first paint
        # A resumed session opens at the bottom (where you left off); a fresh one
        # starts top-aligned with the header pinned at the top and only anchors
        # once a turn's output overflows the viewport (see _anchor_on_overflow).
        if self.harness.session.history:
            log.anchor()
            # Already anchored at the bottom — latch so a later flush won't re-anchor
            # and yank the user back down after they scroll up.
            self.stream._anchored_on_overflow = True
        self._render_tasks()  # reflect any checklist restored with the session
        self._render_jobs()  # process-scoped jobs survive session switches
        self._render_queue()
        # Seed vision capabilities in the background so the text-only-model
        # warning can fire even before the user opens the model picker.
        source = self.harness.model_source
        if source is not None:
            self.run_worker(
                self._refresh_vision_caps(source.list_models), exclusive=False
            )
        # Coalesce streaming text deltas: render buffered AssistantMessages on a
        # shared interval instead of re-parsing the markdown on every token.
        self.set_interval(_STREAM_FLUSH_INTERVAL, self.stream.flush_streams)
        # Anchor the session timer at mount and tick the status bar while idle so
        # the session duration advances even with no turn running.
        self.status.session_start = time.monotonic()
        # Start the active-time clock on a fresh session (resume()/new_session
        # already do it for resumed/new ones).
        self.harness.session.ensure_segment_started()
        self.set_interval(_CLOCK_TICK_INTERVAL, self.status.refresh_status)
        # Animate the working indicator while a turn runs (no-op when idle).
        self.set_interval(_SPINNER_TICK_INTERVAL, self.status.tick_spinner)
        # Land focus on the prompt so the user can type immediately.
        self.query_one(PromptInput).focus()
        await self._connect_mcp(log)
        await self.harness.session_start(
            "resume" if self.harness.session.history else "startup"
        )

    async def _connect_mcp(self, log: VerticalScroll) -> None:
        """Open the configured MCP servers and note the outcome. Connection
        failures are surfaced as a notice, never fatal — the app runs fine with
        the servers that did come up (or none at all)."""
        if not self.harness.mcp.mcp_servers:
            return
        status = await self.harness.connect()
        if status["connected"]:
            await log.mount(
                NoticeMessage(f"MCP connected: {', '.join(status['connected'])}")
            )
        for name, error in status["failed"]:
            await log.mount(ErrorMessage(f"MCP {name} failed: {error}"))

    async def on_unmount(self) -> None:
        """Jobs are process-scoped — kill any still running when the app exits so
        no detached shell or agent run is left behind, and close MCP connections."""
        # Persist session duration before tearing down. Fold this run's active
        # time into the total and force the save: the final segment must land
        # even when history is unchanged (an idle exit would otherwise skip the
        # cache-gated persist and lose it).
        session = self.harness.session
        session.finalize_active_time()
        session.persist(force=True)
        # Show a brief session summary in the terminal after exit.
        total = session.duration_seconds
        usage = session.usage
        total_tokens = usage.input_tokens + usage.output_tokens
        cost, _ = resolve_cost(usage, self.harness.model_id)
        parts = [f"Session: {format_duration(total)}"]
        parts.append(f"Tokens: {human_tokens(total_tokens)}")
        if cost is not None:
            parts.append(f"Cost: {format_cost(cost)}")
        summary = " · ".join(parts)
        # Reset the terminal tab title so a stale "● working" mark doesn't linger
        # after exit. Best-effort: the driver may already be tearing down.
        if self._driver is not None:
            try:
                self._driver.write(osc_title("marim-harness"))
                self._driver.write(f"\r\n{summary}\r\n")
                self._driver.flush()
            except Exception:
                pass
        await self.harness.deps.jobs.cancel_all()
        await self.harness.session_end("exit")
        await self.harness.aclose()

    def _render_tasks(self) -> None:
        """Repaint the task panel from the harness's current checklist."""
        try:
            panel = self.query_one(TaskPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_tasks(self.harness.deps.tasks.items)

    def _on_tasks_changed(self) -> None:
        """Live callback from the update_tasks tool — repaint as the agent edits
        the list mid-turn. Fired on the app's event loop, so it's safe to touch
        widgets directly."""
        self._render_tasks()

    def _render_jobs(self) -> None:
        """Repaint the jobs panel from the registry's current jobs."""
        if not self.is_running:
            return  # a job changed before mount / after teardown — on_mount paints
        try:
            panel = self.query_one(JobPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_jobs(self.harness.deps.jobs.list())

    def _on_jobs_changed(self) -> None:
        """Live callback from the job registry — repaint as jobs launch and
        finish. Each job runs as a task on the app's event loop, so the callback
        fires there and direct widget mutation is safe."""
        self.stream.fill_finished_detached_cards(self.harness.deps.jobs)
        self._render_jobs()
        self._notify_finished_jobs()
        self._maybe_wake()

    def _notify(self, title: str, body: str, event_type: str) -> None:
        """Fire a desktop notification if one is wired on deps. Best-effort —
        the notifier itself swallows all errors, so this is a safe no-op when
        notifications are off or the platform lacks a daemon.

        Dispatched OFF the event loop: the platform notifiers shell out and wait
        (the Windows balloon-tip backend alone sleeps ~5.5s), so calling the
        blocking ``send`` here — from turn-end / approval / job-completion
        callbacks — would freeze the whole UI. We schedule the async send path,
        which spawns the subprocess via asyncio and awaits it without blocking
        other tasks. Failures stay swallowed inside the notifier."""
        notifier = self.harness.deps.notifier
        if notifier is not None:
            self.run_worker(
                notifier.send_async(title, body, event_type),
                name=f"notify:{event_type}",
                group="notifications",
                exit_on_error=False,
            )

    def _notify_finished_jobs(self) -> None:
        """Desktop-notify once per genuinely completed (done/failed) background
        job. Decoupled from the autonomous-wake path so a completion still pings
        when wake is off, a turn is busy, or the depth cap is hit. Cancelled jobs
        are skipped — they're either agent-initiated or shutdown teardown, so a
        ping would be noise (and this keeps ``cancel_all`` on exit silent)."""
        for job in self.harness.deps.jobs.list():
            if job.status in ("done", "failed") and job.id not in self._notified_jobs:
                self._notified_jobs.add(job.id)
                self._notify(
                    "Background job finished",
                    f"{job.id} ({job.kind}) {job.status}",
                    "job_done",
                )

    def _maybe_wake(self) -> None:
        """Fire one digest-only autonomous turn iff a background job has finished
        and nothing is blocking. Guards (all must hold): wake enabled, the turn
        worker is idle, the depth cap is not yet reached, and there is a pending
        finished-job digest. The digest itself is consumed later inside the turn
        by ``_assemble_prompt('')`` -> ``take_finished_digest()`` — this predicate
        only peeks, so a queued digest survives until a turn actually runs."""
        if not self.is_running:
            return  # firing during teardown would race the unmount
        if not self._wake.should_wake(
            enabled=self.autonomous_wake,
            turn_busy=self.turn_busy,
            has_finished_pending=self.harness.deps.jobs.has_finished_pending(),
            all_jobs_settled=not self.harness.deps.jobs.any_running(),
        ):
            return
        self._wake.record_auto_turn()
        # Mounted synchronously (we may be in a sync on_change callback), mirroring
        # _on_compact / _on_rename.
        self._append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)

    @property
    def turn_busy(self) -> bool:
        """True while a turn worker (user submit, drained queue, system command,
        or autonomous wake) is live, OR is mid-spawn (``_turn_starting``). The
        single guard against starting another exclusive turn — which Textual would
        satisfy by silently cancelling the running one — or tearing down the
        conversation/session under it. The ``_turn_starting`` term closes the gap
        before ``_start_turn`` has set ``_turn_worker``."""
        return self._turn_worker is not None or self._turn_starting

    def action_cycle_mode(self) -> None:
        self.harness.cycle_mode()
        self.status.refresh_status()

    def action_toggle_outputs(self) -> None:
        """Ctrl+O: reveal every tool output in full (expand groups, uncap edit
        diffs), or restore the default view on a second press."""
        self.stream.toggle_reveal_all()

    # --- Sub-agent full-screen viewer (ctrl+x) ---

    def action_toggle_subagents(self) -> None:
        """Ctrl+X: open the full-screen sub-agent viewer (or close it if open)."""
        if self.subagent_viewer_open:
            self._close_subagents()
        else:
            self._open_subagents()

    def action_close_subagents(self) -> None:
        """Leave the viewer (bound to up/esc/ctrl+x on the focused side panel)."""
        self._close_subagents()

    def action_subagent_prev(self) -> None:
        if self.subagent_viewer_open:
            self.subagent_index -= 1
            self._apply_subagent_view()

    def action_subagent_next(self) -> None:
        if self.subagent_viewer_open:
            self.subagent_index += 1
            self._apply_subagent_view()

    def _open_subagents(self) -> None:
        subs = self.stream.subagents
        if not subs:
            self.query_one("#log", VerticalScroll).mount(
                NoticeMessage("No sub-agents spawned yet — nothing to view.")
            )
            return
        self.subagent_viewer_open = True
        # Open on the most recent spawn (the one you most likely just watched).
        self.subagent_index = len(subs) - 1
        self.query_one(SubAgentList).display = True
        self.query_one("#subagent-footer", SubAgentFooter).display = True
        self._apply_subagent_view()
        self.query_one(SubAgentList).focus()

    def _close_subagents(self) -> None:
        self.subagent_viewer_open = False
        self.stream.viewing_sid = None
        for w in self.stream.subagents:
            w.body.remove_class("viewing")
            w.body.display = False
        try:
            self.query_one(SubAgentList).display = False
            self.query_one("#subagent-footer", SubAgentFooter).display = False
        except NoMatches:
            pass
        self.query_one(PromptInput).focus()

    def _apply_subagent_view(self) -> None:
        """Reveal the selected sub-agent's transcript in place and repaint the list
        and footer. Clamps the index and closes the viewer if the list is empty."""
        subs = self.stream.subagents
        if not subs:
            self._close_subagents()
            return
        self.subagent_index = max(0, min(self.subagent_index, len(subs) - 1))
        current = subs[self.subagent_index]
        # Exactly one transcript carries the overlay (`viewing`) class + display at a
        # time; the rest stay hidden inline. Never reparented — just toggled.
        for i, w in enumerate(subs):
            if i == self.subagent_index:
                w.body.add_class("viewing")
                w.body.display = True
            else:
                w.body.remove_class("viewing")
                w.body.display = False
        self.stream.viewing_sid = current.stream_id
        self.query_one(SubAgentList).show_subagents(subs, self.subagent_index)
        self.query_one("#subagent-footer", SubAgentFooter).show_status(
            current.agent_type, self.subagent_index, len(subs),
            self._subagent_spend(current),
        )
        # Render the just-revealed transcript now rather than waiting for the next
        # flush tick (its streams were skipped while it wasn't being viewed).
        self.stream.flush_streams()

    def _subagent_spend(self, widget) -> str:
        """A compact ``{tokens} ({pct}%)`` spend tag for the footer, where pct is the
        share of the model's context window; empty until the spawn is metered."""
        if not widget.tokens:
            return ""
        max_ctx = getattr(self.harness.session, "max_context_tokens", 0) or 0
        tag = human_tokens(widget.tokens)
        if max_ctx:
            tag += f" ({round(widget.tokens / max_ctx * 100)}%)"
        return tag

    def watch_theme(self, theme: str) -> None:
        """Persist the active theme so it's the startup theme next run. Only the
        marim themes are saved; Textual may set built-in defaults during init,
        which save_theme ignores."""
        save_theme(theme)

    async def _start_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Mount the user message and spawn the exclusive turn worker. Shared by
        a fresh submit and a drained queue item. Resets the autonomous-wake
        chain and spawns the worker.

        ``_turn_starting`` is latched *before* the first await so a concurrent
        submit can't slip through ``turn_busy`` while we're between the mount and
        the worker being created. Cleared in ``finally`` on every path — the
        worker (once created) carries the busy flag from there on, and on an early
        error there is no worker, so the latch must drop or the UI wedges."""
        self._turn_starting = True
        try:
            self._wake.reset()
            log = self.query_one("#log", VerticalScroll)
            await log.mount(UserMessage(text))
            self.stream.current_assistant = None
            self._turn_worker = self.run_worker(
                self._run_turn(text, attachments), exclusive=True
            )
        finally:
            self._turn_starting = False

    def start_system_turn(self, prompt: str) -> bool:
        """Spawn a turn for a system-initiated prompt — a slash command like
        /remember or /skill that injects its own prompt. Unlike _start_turn it
        mounts no user message and leaves the autonomous-wake chain untouched;
        it just resets the stream and runs the exclusive worker.

        Refused (returns False, no turn started) while a turn is already running:
        the exclusive worker would otherwise silently cancel the in-flight turn
        and race its finally-block bookkeeping. Returns True when the turn was
        started."""
        if self.turn_busy:
            self.query_one("#log", VerticalScroll).mount(
                NoticeMessage(
                    "A turn is already running — wait for it to finish or press Esc."
                )
            )
            return False
        self.stream.current_assistant = None
        self._turn_worker = self.run_worker(self._run_turn(prompt), exclusive=True)
        return True

    def _enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._queue_seq += 1
        self._queue.append(QueuedMessage(text, attachments, str(self._queue_seq)))
        self._render_queue()

    async def _drain_next(self) -> None:
        """Pop and start the next queued message."""
        item = self._queue.pop(0)
        self._render_queue()
        await self._start_turn(item.text, item.attachments)

    async def _after_turn(self) -> None:
        """Called from _run_turn's finally. Drain the next queued item on a
        clean, unpaused turn; otherwise fall through to the background-job wake."""
        # A steer that landed in the finishing gap (never flushed onto a live
        # run) falls back to the front of the queue so it runs next — kept even
        # on a paused (cancel/error) finish, matching how the queue itself is
        # preserved on pause; the drain below stays gated so it waits for resume.
        leftover = self.harness.take_buffered_steers()
        if leftover:
            for text, atts in reversed(leftover):
                self._queue_seq += 1
                self._queue.insert(0, QueuedMessage(text, atts, str(self._queue_seq)))
            self._render_queue()
        # _after_turn runs from _run_turn's finally; an exception escaping here
        # would kill the worker before it unwinds cleanly. Draining starts the
        # next turn (worker scheduling, widget mounts) and the wake path touches
        # jobs — both can fail. Pause the queue and surface the error rather than
        # let it propagate out of the finally and strand the session.
        try:
            if not self._queue_paused and self._queue:
                await self._drain_next()
            else:
                self._maybe_wake()
        except Exception as exc:
            self._queue_paused = True
            self._append_log(ErrorMessage(f"failed to start next turn: {exc}"))

    def _render_queue(self) -> None:
        """Repaint the queue panel from the current queue."""
        if not self.is_running:
            return
        try:
            panel = self.query_one(QueuePanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_queue(self._queue, paused=self._queue_paused)

    async def action_run_queued(self) -> None:
        """Resume a paused queue: clear the pause and start the next item."""
        if self._queue and not self.turn_busy:
            self._queue_paused = False
            await self._drain_next()

    def action_remove_queued(self, id: str) -> None:
        """Drop a pending queued message before it runs."""
        self._queue = [m for m in self._queue if m.id != id]
        self._render_queue()

    async def action_edit_queued(self, id: str) -> None:
        """Pop a queued message out of the queue and load it into the prompt input
        for editing — text and image attachments both, so an edit round-trips
        without losing the images (their ``[Image #N]`` markers ride along in the
        text)."""
        item = next((m for m in self._queue if m.id == id), None)
        if item is None:
            return
        self._queue = [m for m in self._queue if m.id != id]
        self._render_queue()
        prompt = self.query_one(PromptInput)
        prompt.text = item.text
        prompt.load_attachments(item.attachments or [])
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

    def action_cancel_turn(self) -> None:
        if self.status.busy and self._turn_worker is not None:
            self._turn_worker.cancel()

    def _maybe_warn_pending_quit(self) -> bool:
        """Confirm-once guard for quitting with messages still queued. Returns
        True if the quit should be cancelled (a warning was just shown); False
        to let the quit proceed. The queue is process-scoped and dropped on exit,
        so warn the user before discarding pending work."""
        if self._queue and not self._quit_armed:
            self._quit_armed = True
            self.query_one("#log", VerticalScroll).mount(
                NoticeMessage(
                    f"{len(self._queue)} queued message(s) will be discarded. "
                    "Quit again to confirm."
                )
            )
            return True
        return False

    async def action_quit(self) -> None:
        if self._maybe_warn_pending_quit():
            return
        await super().action_quit()

    def _on_compact_start(self) -> None:
        """Show a live note while compaction runs — the summarizer call can take a
        few seconds, which would otherwise be indistinguishable from a slow turn.
        Cleared by _on_compact when the work finishes. Called synchronously from
        run_turn; mount without awaiting."""
        log = self.query_one("#log", VerticalScroll)
        self._compacting_notice = NoticeMessage("compacting conversation…")
        log.mount(self._compacting_notice)

    def _on_compact(self, before: int, after: int) -> None:
        """Note in the log when history was trimmed to stay under the token budget.
        Called synchronously from run_turn; mount without awaiting."""
        log = self.query_one("#log", VerticalScroll)
        if self._compacting_notice is not None:
            self._compacting_notice.remove()  # replace the live "compacting…" line
            self._compacting_notice = None
        # before == after means a (forced) compaction ran without shrinking — the
        # call exists only to clear the indicator above, so don't post a confusing
        # "compacted: N → N" line or re-surface a stale summary.
        if before == after:
            self.status.refresh_status()
            return
        log.mount(
            NoticeMessage(f"compacted history: {before} → {after} messages")
        )
        # Surface the just-created summary as its own collapsed block so the
        # condensed context is legible immediately, not just on the next resume.
        body = self._latest_summary()
        if body is not None:
            log.mount(SummaryWidget(body))
        self.status.refresh_status()  # context gauge shrinks immediately

    def _latest_summary(self) -> "str | None":
        """The body of the most recent compaction summary in history, or None."""
        from ...compaction import summary_text

        found = None
        for message in self.harness.session.history:
            for part in getattr(message, "parts", []):
                body = summary_text(getattr(part, "content", None))
                if body is not None:
                    found = body
        return found

    async def post_system(self, markdown: str) -> None:
        """Render a system/command message into the log (markdown)."""
        log = self.query_one("#log", VerticalScroll)
        msg = AssistantMessage()
        await log.mount(msg)
        self.stream.append_stream(msg, markdown)
        self.stream.flush_streams()  # one-shot system text: render it now, no tick wait

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd).
        Refused mid-turn — clearing would tear down the log the running turn is
        still streaming into and wipe history it is appending to."""
        if self.turn_busy:
            await self.post_system("Can't clear while a turn is running. Press Esc first.")
            return
        await self.session.reset_conversation()

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk. Refused
        mid-turn — switching the active session out from under a running turn
        would race its history persist."""
        if self.turn_busy:
            await self.post_system(
                "Can't start a new session while a turn is running. Press Esc first."
            )
            return
        await self.session.start_new_session(name)

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off. Refused mid-turn
        for the same reason as /new — the running turn writes to the session it
        would be switched away from."""
        if self.turn_busy:
            await self.post_system(
                "Can't switch sessions while a turn is running. Press Esc first."
            )
            return
        await self.session.switch_to_session_id(session_id)

    async def rewind_to_checkpoint(self, index: int) -> None:
        """Rewind the session to checkpoint ``index`` and rebuild the log.
        Refused mid-turn — rewinding under a running turn would race history."""
        if self.status.busy:
            await self.post_system("Can't rewind while a turn is running. Press Esc first.")
            return
        try:
            result = self.harness.checkpoints.rewind(index)
        except KeyError:
            await self.post_system(f"No checkpoint #{index}. Try `/rewind` to list them.")
            return
        note = f"rewound to checkpoint #{index}"
        if result.restored_files:
            note += " (files restored)"
        elif result.restore_failed:
            note += (
                " — ⚠ file restore failed; the working tree may be partial "
                "(`/rewind undo` to recover the pre-rewind state)"
            )
        await self.session.render_session(note)
        self.status.refresh_status()

    async def undo_rewind(self) -> None:
        """Undo the last rewind, restoring the conversation (and the working tree, if
        the rewind touched files) to their pre-rewind state. Re-renders the log since
        the conversation changed. Refused mid-turn."""
        if self.status.busy:
            await self.post_system(
                "Can't undo a rewind while a turn is running. Press Esc first."
            )
            return
        if self.harness.checkpoints.undo_rewind():
            await self.session.render_session(
                "undid the rewind — restored the pre-rewind conversation and files"
            )
            self.status.refresh_status()
        else:
            await self.post_system("Nothing to undo — no rewind in this session.")

    def open_settings(self) -> None:
        """Open the settings modal: runtime settings apply live; env-backed
        settings save to the global .env on demand."""
        from ...config import load_config

        self.push_screen(
            SettingsModal(
                harness=self.harness,
                current_theme=self.theme,
                env_cfg=load_config(),
            )
        )

    async def open_model_picker(self) -> None:
        """Open the picker and let the user choose a model, applying the choice to
        the harness. The catalog loads inside the modal's own worker, so the
        picker appears instantly even on a slow provider; it degrades to free-text
        when no catalog loads.

        Uses the callback form of push_screen (not push_screen_wait) so it works
        when called straight from the command-dispatch path, which is not a
        worker — push_screen_wait would raise NoActiveWorker there.
        """
        source = self.harness.model_source
        if source is None:
            await self.post_system("Model switching isn't available here.")
            return
        self.run_worker(self._refresh_vision_caps(source.list_models),
                        exclusive=False)
        self.push_screen(
            ModelPickerModal(
                current=self.harness.model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_model_chosen,
        )

    async def _refresh_vision_caps(self, fetch) -> None:
        try:
            entries = await fetch()
        except Exception:
            return  # unknown stays unknown; never blocks submit
        self._vision_caps = {e.id: e.supports_images for e in entries}

    def _on_model_chosen(self, chosen: str | None) -> None:
        """Apply a model selected in the picker. Invoked by push_screen when the
        modal is dismissed; a None result (cancelled) is a no-op."""
        if not chosen:
            return
        self.harness.set_model(chosen)
        self.status.refresh_status()
        self._append_log(NoticeMessage(f"model: {self.harness.model_label}"))

    def _append_log(self, widget) -> None:
        """Mount a notice/error into the log, keeping the viewport pinned to the
        bottom only if it was already there. A user who scrolled up to read history
        isn't yanked back down, but a user following live still sees new messages
        (errors, steering echoes) scroll into view instead of landing off-screen."""
        log = self.query_one("#log", VerticalScroll)
        at_bottom = log.scroll_offset.y >= log.max_scroll_y
        log.mount(widget)
        if at_bottom:
            log.scroll_end(animate=False)

    def _image_block_reason(self, attachments) -> str | None:
        """A warning to show instead of submitting, or None to proceed. Only a
        positive text-only capability blocks; unknown always proceeds."""
        if not attachments:
            return None
        model_id = self.harness.model_id
        if model_id is not None and self._vision_caps.get(model_id) is False:
            return (f"{model_id} can't read images — "
                    "switch to a vision model with /model or remove the image.")
        return None

    async def _request_approval(self, call) -> DeferredToolApprovalResult | bool:
        self._notify(
            "Approval needed",
            f"Tool: {call.tool_name}",
            "approval_needed",
        )
        approved = await self.push_screen_wait(
            ApprovalModal(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")

    async def _ask_user(self, questions):
        """Put a structured question to the user and return their {header:
        answer} mapping, or None if they dismissed it. Runs inside the turn
        worker, so push_screen_wait is valid (same as _request_approval)."""
        prompt = questions[0].question if questions else ""
        self._notify("Question from agent", prompt, "ask_user")
        return await self.push_screen_wait(AskUserModal(questions))

    # --- Slash-command autocomplete ---

    def _show_autocomplete(self, query: str) -> None:
        if self._autocomplete is None:
            self._autocomplete = self.query_one("#cmd-autocomplete", CommandAutocomplete)
        self._autocomplete.filter(query)

    def _hide_autocomplete(self) -> None:
        if self._autocomplete is not None:
            self._autocomplete.visible = False

    def autocomplete_navigate(self, delta: int) -> bool:
        """Move the open slash-menu's highlight (the prompt forwards Up/Down here
        while the menu is showing). Returns True when it consumed the key."""
        if self._autocomplete is None:
            return False
        return self._autocomplete.move_highlight(delta)

    def autocomplete_accept(self) -> bool:
        """Complete the highlighted slash command into the prompt (the prompt
        forwards Tab here while the menu is showing). Returns True when a command
        was filled in, False when there's nothing to accept."""
        if self._autocomplete is None:
            return False
        return self._autocomplete.accept_highlighted()

    def on_prompt_input_slash_changed(
        self, event: PromptInput.SlashChanged
    ) -> None:
        first_line = event.value.split("\n", 1)[0]
        query = first_line[1:]  # strip the leading /
        self._show_autocomplete(query)

    def on_prompt_input_slash_dismissed(
        self, _event: PromptInput.SlashDismissed
    ) -> None:
        self._hide_autocomplete()

    def on_command_autocomplete_command_selected(
        self, event: CommandAutocomplete.CommandSelected
    ) -> None:
        prompt = self.query_one(PromptInput)
        prompt.text = f"/{event.command_name} "
        prompt.move_cursor(prompt.document.end)
        self._hide_autocomplete()
        prompt.focus()

    async def on_prompt_input_steer(self, event: PromptInput.Steer) -> None:
        text = event.value.strip()
        if not text and not event.attachments:
            return  # nothing to steer
        if not self.turn_busy:
            # No turn running (or starting) — just run it normally.
            await self._start_turn(text, event.attachments)
            return
        reason = self._image_block_reason(event.attachments)
        if reason is not None:
            self._append_log(NoticeMessage(reason))
            return
        self.harness.steer(text, event.attachments)
        tag = f"  📎 {len(event.attachments)}" if event.attachments else ""
        self._append_log(NoticeMessage(f"↪ steering: {text}{tag}"))

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        self._hide_autocomplete()
        text = event.value.strip()
        if not text:
            return
        self._history.add(text)  # capture every submission, commands included
        self.query_one(PromptInput).text = ""
        if text.startswith("/"):
            await dispatch(self, text)
            return
        reason = self._image_block_reason(event.attachments)
        if reason is not None:
            self._append_log(NoticeMessage(reason))
            return
        if self.turn_busy:
            # turn_busy (not _turn_worker) so a submit landing in the start-up gap
            # is queued rather than racing a second exclusive worker.
            self._enqueue(text, event.attachments)
            return
        self._queue_paused = False
        await self._start_turn(text, event.attachments)

    async def _run_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        self.status.turn_start = time.monotonic()
        self.status.set_busy(True)
        # Drop finished tool-widget entries from the prior turn(s) so the per-turn
        # tracking dict doesn't grow unbounded across a long session. Done at the
        # turn boundary (not per approval round) so the within-turn duplicate guard
        # for gated tools keeps its entries while the turn is live.
        self.stream.prune_completed()
        log = self.query_one("#log", VerticalScroll)
        try:
            await self.harness.run_turn(
                text, event_stream_handler=self.stream.on_events, attachments=attachments
            )
            # Stamp the just-finished turn's duration under its reply (success
            # only; cancelled/errored turns surface an ErrorMessage instead).
            elapsed = format_duration(time.monotonic() - self.status.turn_start, precise=True)
            await log.mount(TurnMeta(elapsed))
            self._notify("Turn complete", f"Finished in {elapsed}", "turn_complete")
        except CancelledError:
            # User pressed escape; mount synchronously (we are unwinding) and
            # let the worker finish as cancelled.
            self._queue_paused = True
            self._append_log(ErrorMessage("turn cancelled"))
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            self._queue_paused = True
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            self._append_log(ErrorMessage(detail))
            self._notify("Turn error", detail, "error")
        finally:
            self._turn_worker = None
            self.status.set_busy(False)
            await self._after_turn()  # drain next queued item, or wake on jobs
