import time
from asyncio import CancelledError

import rich.markup
from pydantic_ai import ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header, Static

from ...jobs import JobRegistry
from ...runtime.errors import format_provider_error
from ...runtime.harness import Harness
from ...usage import resolve_cost
from ..history import PromptHistory
from ..prefs import load_theme, save_theme
from .commands import dispatch
from .interactions import ApprovalPanel, AskUserPanel, InteractionPanel, PlanCard, run_panel
from .model_picker import ModelPickerModal
from .notify import FinishedJobNotifier
from .queue import TurnQueue
from .session_view import SessionView
from .settings import SettingsScreen
from .shell_passthrough import (
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    run_passthrough,
)
from .status import (
    _CLOCK_TICK_INTERVAL,
    _SPINNER_TICK_INTERVAL,
    StatusPresenter,
    format_duration,
    osc_title,
)
from .stream_render import StreamRenderer
from .subagents import SubAgentsScreen, SubAgentsView
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

# A second quit attempt (ctrl+c, /exit, /quit) within this many seconds of the
# first confirms the quit; after it elapses the next attempt warns again. A
# short deliberate window, not a latch, so the warning always resurfaces after
# real inactivity instead of being spent once and forgotten for the rest of
# the process.
_QUIT_CONFIRM_WINDOW = 2.0

_WELCOME = (
    "Type a message below to start, or `/help` for commands.\n\n"
    "- `/` opens the command menu — `↑`/`↓` to move, `tab` to complete\n"
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+v` attaches a copied image (the terminal's own paste is text-only)\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `ctrl+g` (or `alt+enter`) steers the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits — `ctrl+c` requires a double-press to confirm"
)


class HarnessApp(App):
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("ctrl+o", "toggle_outputs", "Show all output"),
        ("ctrl+x", "toggle_subagents", "Subagents"),
        ("ctrl+p", "show_plan", "Plan"),
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
            on_present_plan=self._present_plan,
            on_workflow_spawn=self._on_workflow_spawn,
            on_workflow_start=self.stream.claim_workflow_card,
            on_workflow_log=self._on_workflow_log,
            on_workflow_done=self.stream.finish_workflow_card,
            on_workflow_spawn_done=self.stream.finish_workflow_child,
            on_subagent_event=self.stream.on_subagent_event,
            on_subagent_notice=self.stream.on_subagent_notice,
            on_subagent_model=self.stream.on_subagent_model,
            on_subagent_usage=self.stream.on_subagent_usage,
            on_cli_activity=self.stream.on_cli_activity,
            on_ttft=self.stream.on_ttft,
            on_mode_change=self._refresh_mode_display,
            on_tasks_changed=self._on_tasks_changed,
            on_jobs_changed=self._on_jobs_changed,
            on_compact=self._on_compact,
            on_compact_start=self._on_compact_start,
            on_notice=self._on_session_notice,
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
        # True while the /compact worker (group "compact") is mid-run. A
        # summarize can take seconds, and starting a turn or rebinding the
        # session store under it risks silent turn loss, cross-session history
        # contamination (a late `self.history=…; persist()` after the store was
        # swapped), and the only path to concurrent persist_elided calls. So the
        # session-teardown/turn-start flows gate on it, symmetric with the guard
        # /compact itself applies against turn_busy. Set in _cmd_compact, cleared
        # in its worker's finally.
        self.compact_busy = False
        self._queue = TurnQueue()
        # Confirm-to-quit guard (see _QUIT_CONFIRM_WINDOW): timestamp of the last
        # unconfirmed quit attempt, or None if there isn't one outstanding.
        self._quit_warned_at: float | None = None
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off`.
        self.autonomous_wake = harness.autonomous_wake
        # Bounds the wake→spawn→wake chain and owns the should-wake decision; the
        # App keeps the public autonomous_wake toggle and the wake's side effects.
        self._wake = WakeController(harness.wake_depth_cap)
        # Dedup tracker: pings each finished job exactly once, independent of
        # the autonomous-wake path.
        self._job_notifier = FinishedJobNotifier()
        self._autocomplete: CommandAutocomplete | None = None
        # Full-bleed sub-agents screen (ctrl+x): its open/navigate/close lifecycle
        # and the per-frame repaint coalescing live in this collaborator.
        self.subagents = SubAgentsScreen(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield QueuePanel()
        yield Static(self.status.status_text(), id="status-bar")
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput(history=self._history)
        # The full-bleed sub-agents screen (hidden until ctrl+x). Its detail host
        # owns the live transcript panes; the renderer mounts each spawn's stream
        # into them whether or not the screen is open, so opening mid-run shows an
        # already-current transcript.
        yield SubAgentsView()
        yield Footer()

    async def on_mount(self) -> None:
        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.theme = load_theme()
        self.sub_title = str(self.harness.deps.workspace.root)
        self.status.refresh_title()
        log = self.query_one("#log", VerticalScroll)
        # Hand the renderer the persistent transcript host so spawns create their
        # panes there.
        self.stream.detail_host = self.query_one(SubAgentsView).host
        intro = await self.session.mount_header(log)
        if self.harness.session.history:
            n = len(self.harness.session.history)
            tokens = self.harness.session.total_tokens
            self.stream.append_stream(
                intro,
                f"**Resumed session** — {n} messages, {tokens} tokens restored.",
            )
        else:
            self.stream.append_stream(intro, _WELCOME)
        # Replay the restored history AND settle its sub-agent cards through the
        # same seam the switch/clear path uses (SessionView.replay_and_settle).
        # Routing startup resume through it is what makes a spawn killed mid-run
        # surface here as an interrupted card — replaying alone (the old behavior)
        # left the killed spawn's sidecar unsettled and the card invisible.
        await self.session.replay_and_settle(log)
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
        # One-line advisor status at session start, so an active advisor (env
        # default or session-persisted) is visible without opening settings.
        if self.harness.advisor_model_id is not None:
            self._append_log(
                NoticeMessage(f"Advisor: {self.harness.advisor_model_id} · /advisor")
            )
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

    def on_descendant_focus(self, event: events.DescendantFocus) -> None:
        """Keep the prompt focused. When focus lands on a non-input main-screen
        widget — the conversation transcript or a panel header, reachable by a
        click or Tab — snap it straight back to the prompt so a keystroke always
        lands in the input. Three scopes are deliberately left alone: a pushed
        modal/overlay (it's a separate screen, ``screen_stack > 1``, and owns its
        own focus), the ctrl+x sub-agents screen (``subagents.open``, which
        drives its list/pane focus), and an active InteractionPanel (ask-user/
        approval) — unlike the ModalScreens it replaces, it's mounted in this
        same base screen, so its OptionList/SelectionList/buttons need this
        guard to back off or Enter/Space could never reach them. Refocusing the
        prompt re-fires this for the prompt itself, which the identity check
        below makes a no-op — no loop."""
        if len(self.screen_stack) > 1 or self.subagents.open or self.query(InteractionPanel):
            return
        try:
            prompt = self.query_one(PromptInput)
        except NoMatches:
            return
        if event.widget is not prompt:
            prompt.focus()

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
        # Don't hold exit hostage to an in-flight background autoname (a titler
        # LLM call). auto_named stays True, so the next resume simply retries.
        session.cancel_autoname()
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
        await self.jobs.cancel_all()
        await self.harness.session_end("exit")
        await self.harness.aclose()

    def _render_tasks(self) -> None:
        """Repaint the task panel from the harness's current checklist, plus a
        compact plan title when a plan has been presented this session."""
        try:
            panel = self.query_one(TaskPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        plan = self.harness.deps.plan
        panel.show_tasks(
            self.harness.deps.tasks.items,
            plan_title=plan.summary if plan is not None else None,
        )

    def _on_tasks_changed(self) -> None:
        """Live callback from the update_tasks tool — repaint as the agent edits
        the list mid-turn. Fired on the app's event loop, so it's safe to touch
        widgets directly."""
        self._render_tasks()

    def _render_jobs(self) -> None:
        """Repaint the jobs panel from the registry's current jobs, prior-session
        history first (history rows are terminal, so render_jobs already
        suffixes them ``(done)``/``(failed)``)."""
        if not self.is_running:
            return  # a job changed before mount / after teardown — on_mount paints
        try:
            panel = self.query_one(JobPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_jobs(self.jobs.history + self.jobs.list())

    def _on_jobs_changed(self) -> None:
        """Live callback from the job registry — repaint as jobs launch and
        finish. Each job runs as a task on the app's event loop, so the callback
        fires there and direct widget mutation is safe."""
        self.stream.fill_finished_detached_cards(self.jobs)
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
        notifier = self.harness.deps.ui.notifier
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
        for job in self._job_notifier.newly_finished(self.jobs.list()):
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
            has_finished_pending=self.jobs.has_finished_pending(),
            all_jobs_settled=not self.jobs.any_running(),
        ):
            return
        self._wake.record_auto_turn()
        # Mounted synchronously (we may be in a sync on_change callback), mirroring
        # _on_compact / _on_rename.
        self._append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)

    @property
    def jobs(self) -> JobRegistry:
        return self.harness.deps.jobs

    @property
    def turn_busy(self) -> bool:
        """True while a turn worker (user submit, drained queue, system command,
        or autonomous wake) is live, OR is mid-spawn (``_turn_starting``). The
        single guard against starting another exclusive turn — which Textual would
        satisfy by silently cancelling the running one — or tearing down the
        conversation/session under it. The ``_turn_starting`` term closes the gap
        before ``_start_turn`` has set ``_turn_worker``."""
        return self._turn_worker is not None or self._turn_starting

    def _refresh_mode_display(self) -> None:
        """Redraw the status bar to reflect the current mode.

        Called from action_cycle_mode, the /mode command (via its own
        app.status.refresh_status() call in commands.py), and via the
        on_mode_change UIHooks callback so a tool that flips workspace.mode
        mid-turn (e.g. present_plan) can nudge the status bar to redraw.
        Runs on the event-loop thread: the exclusive turn worker is an asyncio
        task, so no call_from_thread marshalling is needed — Textual widget
        mutations from asyncio tasks are safe.
        """
        self.status.refresh_status()

    def action_cycle_mode(self) -> None:
        self.harness.cycle_mode()
        self._refresh_mode_display()

    def action_toggle_outputs(self) -> None:
        """Ctrl+O: reveal every tool output in full (expand groups, uncap edit
        diffs), or restore the default view on a second press."""
        self.stream.toggle_reveal_all()

    # --- Sub-agents screen (ctrl+x) — driven by the SubAgentsScreen collaborator ---

    def action_toggle_subagents(self) -> None:
        self.subagents.toggle()

    def action_close_subagents(self) -> None:
        self.subagents.close()

    def action_show_plan(self) -> None:
        """Open the full plan overlay, or flash a hint when no plan exists yet."""
        from .plan_screen import PlanScreen

        plan = self.harness.deps.plan
        if plan is None:
            self.notify("No plan yet — the agent presents one in plan mode.",
                        severity="information")
            return
        self.push_screen(
            PlanScreen(plan.summary, plan.path, self.harness.deps.tasks.items)
        )

    def on_data_table_row_highlighted(self, event) -> None:
        # Textual bubbles the DataTable message to the App; forward to the viewer.
        self.subagents.on_row_highlighted(event)

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
        # Mirror _start_turn's discipline: keep the spawn exception-safe. This path
        # has no awaits (so no concurrent submit can interleave, hence no
        # _turn_starting latch is needed), but resetting the stream or creating the
        # worker could still raise if Textual is mid-teardown. If it does, leave no
        # half-set busy state behind (_turn_worker stays/returns to None so
        # turn_busy doesn't wedge) and report failure rather than letting the
        # exception escape into the slash-command dispatcher.
        try:
            self.stream.current_assistant = None
            self._turn_worker = self.run_worker(self._run_turn(prompt), exclusive=True)
        except Exception:  # noqa: BLE001 — a failed spawn must not wedge the UI
            self._turn_worker = None
            self.log.error("failed to start system turn")
            self._append_log(
                NoticeMessage("Couldn't start the command — please try again.")
            )
            return False
        return True

    def _enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._queue.enqueue(text, attachments)
        self._render_queue()

    async def _drain_next(self) -> None:
        """Pop and start the next queued message."""
        item = self._queue.pop_next()
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
                self._queue.prepend(text, atts)
            self._render_queue()
        # _after_turn runs from _run_turn's finally; an exception escaping here
        # would kill the worker before it unwinds cleanly. Draining starts the
        # next turn (worker scheduling, widget mounts) and the wake path touches
        # jobs — both can fail. Pause the queue and surface the error rather than
        # let it propagate out of the finally and strand the session.
        try:
            if not self._queue.paused and self._queue:
                await self._drain_next()
            else:
                self._maybe_wake()
        except Exception as exc:
            self._queue.paused = True
            self._append_log(ErrorMessage(f"failed to start next turn: {exc}"))

    def _render_queue(self) -> None:
        """Repaint the queue panel from the current queue."""
        if not self.is_running:
            return
        try:
            panel = self.query_one(QueuePanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_queue(self._queue.items, paused=self._queue.paused)

    async def action_run_queued(self) -> None:
        """Resume a paused queue: clear the pause and start the next item."""
        if self._queue and not self.turn_busy:
            self._queue.paused = False
            await self._drain_next()

    def action_remove_queued(self, id: str) -> None:
        """Drop a pending queued message before it runs."""
        self._queue.remove(id)
        self._render_queue()

    async def action_edit_queued(self, id: str) -> None:
        """Pop a queued message out of the queue and load it into the prompt input
        for editing — text and image attachments both, so an edit round-trips
        without losing the images (their ``[Image #N]`` markers ride along in the
        text)."""
        item = self._queue.take(id)
        if item is None:
            return
        self._render_queue()
        prompt = self.query_one(PromptInput)
        prompt.text = item.text
        prompt.load_attachments(item.attachments or [])
        # Drop the paste stash along with the old draft: it belongs to whatever
        # was in the box before, and a stale entry would leave a dangling
        # [Pasted text #N] marker (or make a hand-typed #1 resurrect it).
        prompt.pastes = []
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

    def action_cancel_turn(self) -> None:
        if self.status.busy and self._turn_worker is not None:
            self._turn_worker.cancel()

    def _maybe_warn_pending_quit(self) -> bool:
        """Confirm-to-quit guard against an accidental Ctrl+C. Returns True if
        the quit should be cancelled (a warning was just shown); False to let a
        second attempt within _QUIT_CONFIRM_WINDOW of the first proceed. Always
        warns on the first attempt, even with an empty queue — a stray keypress
        is just as disruptive either way."""
        now = time.monotonic()
        if (
            self._quit_warned_at is not None
            and now - self._quit_warned_at <= _QUIT_CONFIRM_WINDOW
        ):
            return False
        self._quit_warned_at = now
        if self._queue:
            message = (
                f"{len(self._queue.items)} queued message(s) will be discarded. "
                "Quit again to confirm."
            )
        else:
            message = "Quit again to confirm."
        self.query_one("#log", VerticalScroll).mount(NoticeMessage(message))
        return True

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

    def clear_compacting_notice(self) -> None:
        """Remove the live "compacting…" indicator if it is still mounted.
        Idempotent and exception-safe. Called from both the turn worker's finally
        and the /compact worker's error path so a ``maybe_compact`` that raised
        between ``on_compact_start()`` and ``on_compact()`` can never leave the
        notice stranded on screen forever."""
        if self._compacting_notice is not None:
            try:
                self._compacting_notice.remove()
            except ValueError:
                pass  # widget already removed; safe to ignore
            finally:
                self._compacting_notice = None

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

    def _on_session_notice(self, message: str) -> None:
        """Session-level advisory (breaker tripped, manual compact blocked).
        Same call-from-anywhere contract as _on_compact."""
        self._append_log(NoticeMessage(message))

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
        if self.compact_busy:
            await self.post_system("Compaction in progress — wait for it to finish.")
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
        if self.compact_busy:
            await self.post_system("Compaction in progress — wait for it to finish.")
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
        if self.compact_busy:
            await self.post_system("Compaction in progress — wait for it to finish.")
            return
        await self.session.switch_to_session_id(session_id)

    async def rewind_to_checkpoint(self, index: int) -> None:
        """Rewind the session to checkpoint ``index`` and rebuild the log.
        Refused mid-turn — rewinding under a running turn would race history.
        Checks both busy flags: ``turn_busy`` covers the turn worker, and
        ``status.busy`` guards any other flow that marks the app busy without it."""
        if self.turn_busy or self.status.busy:
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
        the conversation changed. Refused mid-turn (same double-flag check as
        ``rewind_to_checkpoint``)."""
        if self.turn_busy or self.status.busy:
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
        """Open the full-bleed settings screen: runtime settings apply live;
        env-backed settings save to the global .env on demand."""
        from ...config import load_config

        self.push_screen(
            SettingsScreen(
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
        self._vision_caps = {e.qualified: e.supports_images for e in entries}

    def _on_model_chosen(self, chosen: str | None) -> None:
        """Apply a model selected in the picker. Invoked by push_screen when the
        modal is dismissed; a None result (cancelled) is a no-op."""
        if not chosen:
            return
        self.harness.set_model(chosen)
        self.status.refresh_status()
        self._append_log(NoticeMessage(f"model: {self.harness.model_label}"))

    async def open_advisor_picker(self) -> None:
        """Model picker for the advisor. Mirrors open_model_picker, but the
        choice lands on the advisor seam (session-persisted) rather than the
        live turn model."""
        source = self.harness.model_source
        if source is None:
            await self.post_system("Model switching isn't available here.")
            return
        self.push_screen(
            ModelPickerModal(
                current=self.harness.advisor_model_id,
                fetch=source.list_models,
                is_local=source.is_local,
            ),
            self._on_advisor_chosen,
        )

    def _on_advisor_chosen(self, chosen: str | None) -> None:
        if not chosen:
            return
        # A typed "off" in the free-text picker means "disable", same as
        # `/advisor off` and the settings picker — map it to None (the seam's
        # off state), never persist the literal "off" as a model id (which
        # would leave the seam active and every consult failing to build it).
        if chosen.strip().lower() == "off":
            self.harness.set_advisor_model(None)
            self._append_log(NoticeMessage("advisor: off"))
            return
        self.harness.set_advisor_model(chosen)
        self._append_log(NoticeMessage(f"advisor: {chosen}"))

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
        approved = await run_panel(
            self, ApprovalPanel(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")

    async def _ask_user(self, questions):
        """Put a structured question to the user and return their {header:
        answer} mapping, or None if they dismissed it. Inline panel, not a
        modal: the transcript stays scrollable while the agent waits, and a
        cancelled turn removes the panel via run_panel's finally."""
        prompt = questions[0].question if questions else ""
        self._notify("Question from agent", prompt, "ask_user")
        return await run_panel(self, AskUserPanel(questions))

    async def _present_plan(self, summary, steps, choices):
        """Put the finished plan to the user as an inline card and return a
        PlanDecision (the chosen execution label, or "Keep planning" with revise-feedback).
        Inline panel, not a modal — the transcript stays scrollable; a cancelled turn
        removes the card via run_panel's finally. The plan's summary/steps already live
        on deps.plan (set by present_plan), so the pinned title and Ctrl+P overlay stay
        in sync regardless of the choice made here."""
        self._notify("Plan ready", summary, "ask_user")
        self._render_tasks()  # refresh the TaskPanel title now that deps.plan is set
        return await run_panel(self, PlanCard(summary, steps, choices))

    async def _on_workflow_spawn(
        self, stream_id: str, type_: str, task: str, parent_id: str
    ) -> None:
        """Claim a card for a workflow-spawned sub-agent (see bind_ui). Fired on
        the app's event loop by the workflow engine before it launches the child,
        so — like on_subagent_event — direct widget mutation via the renderer is
        safe with no call_from_thread marshalling."""
        await self.stream.claim_workflow_spawn(stream_id, type_, task, parent_id)

    def _on_workflow_log(self, tool_call_id: str, message: str) -> None:
        """Route a workflow script's log() line: persist it into the run
        card's pane (so it survives past the toast) and raise the transient
        toast. Fired on the app's event loop by the engine, so direct
        renderer mutation is safe — same as _on_workflow_spawn."""
        self.stream.append_workflow_log(tool_call_id, message)
        self.notify(rich.markup.escape(message), title="workflow", timeout=4)

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
            # No turn running (or starting) — an idle steer is just a
            # submission, so it takes the same path as Enter: history recall,
            # slash/! routing, image gate. Bypassing that sent "/help" to the
            # model as prose and lost the entry from prompt history.
            self._hide_autocomplete()
            self._history.add(text)
            await self._route_submission(text, event.attachments)
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
        await self._route_submission(text, event.attachments)

    async def _route_submission(
        self, text: str, attachments: list[tuple[bytes, str]] | None
    ) -> None:
        """Shared routing for submitted prompt text (Enter and idle steer):
        slash commands, `!` passthrough, image gate, then queue-or-start."""
        if text.startswith("/"):
            await dispatch(self, text)
            return
        if (command := parse_bang(text)) is not None:
            await self._handle_bang(command)
            return
        reason = self._image_block_reason(attachments)
        if reason is not None:
            self._append_log(NoticeMessage(reason))
            return
        if self.compact_busy:
            # Refuse (don't enqueue) so the turn isn't silently lost or run against
            # a session the compact worker is mid-summarize on. Symmetric with the
            # notice /compact posts when a turn is running.
            self._append_log(
                NoticeMessage("Compaction in progress — wait for it to finish.")
            )
            return
        if self.turn_busy:
            # turn_busy (not _turn_worker) so a submit landing in the start-up gap
            # is queued rather than racing a second exclusive worker.
            self._enqueue(text, attachments)
            return
        self._queue.paused = False
        await self._start_turn(text, attachments)

    async def _handle_bang(self, command: str) -> None:
        """Route a `!` submission: usage hint for a bare `!`, refusal mid-turn,
        otherwise run in a worker. A worker (not this handler) because sudo's
        modal needs push_screen_wait — invalid outside a worker, the same
        constraint the model picker documents — and because the command may
        legitimately run for up to PASSTHROUGH_TIMEOUT."""
        if not command:
            await self.post_system(
                "Usage: `! <command>` — run a shell command here; its output is "
                "shared with the model on your next message."
            )
            return
        if self.turn_busy:
            self._append_log(NoticeMessage(
                "Can't run a shell command while a turn is running. "
                "Press Esc first."
            ))
            return
        # group="shell-passthrough": Textual's WorkerManager cancels every worker
        # sharing a group when a new *exclusive* worker joins that group. The turn
        # worker (_start_turn) runs exclusive=True in the default group, so leaving
        # this one there too would let a chat message silently kill an in-flight
        # `!` command with no notice and no queued output. Its own group keeps it
        # immune to that sweep; a turn starting mid-passthrough is fine — the
        # passthrough's output still lands in the transcript and queues normally.
        self.run_worker(
            self._run_shell_passthrough(command),
            group="shell-passthrough",
            exclusive=False,
            # Belt for anything the except clauses in _run_shell_passthrough miss:
            # an arbitrary user command (up to PASSTHROUGH_TIMEOUT) must never be
            # able to take down the whole session via Textual's default
            # exit_on_error=True (see the notification worker below for the same
            # pattern).
            exit_on_error=False,
        )

    async def _run_shell_passthrough(self, command: str) -> None:
        """Execute a `!` command, render its output into the transcript, and
        queue it for the next turn's context. Leading-sudo commands collect a
        password first; it only ever transits the subprocess stdin pipe."""
        password: str | None = None
        if needs_sudo_password(command):
            password = await self.push_screen_wait(SudoPasswordModal(command))
            if password is None:
                self._append_log(NoticeMessage("sudo command cancelled"))
                return
        try:
            output = await run_passthrough(
                self.harness.deps.workspace.root, command, password
            )
        except OSError as exc:
            self._append_log(ErrorMessage(f"! {command} failed to start: {exc}"))
            return
        try:
            # Queue before rendering: if the render below fails, the model still
            # gets the output on the next turn even though the transcript never
            # showed it — losing the render is recoverable (the user can scroll
            # up or re-run), losing the model-context entry silently is worse.
            self.harness.add_shell_result(command, output)
            await self.post_system(format_transcript_block(command, output))
        except Exception as exc:  # keep the session alive on any render failure
            self._append_log(ErrorMessage(f"! {command}: {type(exc).__name__}: {exc}"))

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
            self._queue.paused = True
            self._append_log(ErrorMessage("turn cancelled"))
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            self._queue.paused = True
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            self._append_log(ErrorMessage(detail))
            self._notify("Turn error", detail, "error")
        finally:
            self._turn_worker = None
            self.status.set_busy(False)
            # Guard against an orphaned compaction notice if maybe_compact raised
            # between on_compact_start() and on_compact(). Always try to clean up.
            self.clear_compacting_notice()
            await self._after_turn()  # drain next queued item, or wake on jobs
