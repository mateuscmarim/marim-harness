import logging
import time
from asyncio import CancelledError
from contextlib import suppress

import rich.markup
from pydantic_ai import ToolDenied
from pydantic_ai.tools import DeferredToolApprovalResult
from textual import events
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.css.query import NoMatches
from textual.widgets import Footer, Header

from ...jobs import JobRegistry
from ...runtime.errors import format_provider_error
from ...runtime.harness import Harness
from ...usage import resolve_cost
from ..history import PromptHistory
from ..prefs import load_theme, save_theme
from .activity import ActivityMonitor
from .commands import dispatch
from .interactions import (
    ApprovalPanel,
    AskUserPanel,
    InteractionPanel,
    PlanCard,
    run_panel,
)
from .pickers import ModelPickers
from .queue_control import QueueController
from .session_picker import SessionPickerModal
from .session_view import SessionView
from .settings import SettingsScreen
from .shell_passthrough import (
    SudoPasswordModal,
    format_transcript_block,
    needs_sudo_password,
    parse_bang,
    run_passthrough,
)
from .stream_render import StreamRenderer
from .subagents import SubAgentsScreen, SubAgentsView
from .themes import MARIM_THEMES
from .trust_flow import prompt_project_trust
from .widgets import (
    AssistantMessage,
    CommandAutocomplete,
    ErrorMessage,
    JobPanel,
    NoticeMessage,
    PromptInput,
    TaskPanel,
    TurnMeta,
    UserMessage,
    format_cost,
    human_tokens,
)
from .widgets.compact_notice import CompactNotice
from .widgets.format import _CLOCK_TICK_INTERVAL, _SPINNER_TICK_INTERVAL, format_duration
from .widgets.queue_display import QueueDisplay
from .widgets.status_bar import StatusBar, osc_title

logger = logging.getLogger(__name__)

# How often (seconds) buffered streaming text is rendered. ~12 flushes/sec reads
# as smooth while collapsing many per-token markdown re-parses into one.
_STREAM_FLUSH_INTERVAL = 0.08

# A second ctrl+c within this many seconds of the first confirms the quit; after
# it elapses the next attempt warns again. A short deliberate window, not a
# latch, so the warning always resurfaces after real inactivity instead of being
# spent once and forgotten for the rest of the process.
_QUIT_CONFIRM_WINDOW = 2.0

# The same confirmation for a *typed* quit (/exit, /quit). Much wider, because
# re-typing a command is far slower than double-tapping a key — 2s would make
# the confirmation unreachable and so effectively a hard block.
_TYPED_QUIT_CONFIRM_WINDOW = 20.0

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
    """The interactive TUI.

    This class is the Textual surface — bindings, compose, message handlers,
    actions — plus the turn lifecycle, which is the one thing every other part
    of the UI is timed against. Concerns with their own state and their own
    invariants are collaborators constructed here and reachable as attributes:

    - ``stream`` renders the model's output (stream_render.py)
    - ``session`` rebuilds the log across new/switch/clear (session_view.py)
    - ``queue`` holds submissions made mid-turn (queue_control.py)
    - ``activity`` owns the panels, notifications and wake (activity.py)
    - ``pickers`` opens the live model/advisor/thinking pickers (pickers.py)
    - ``subagents`` drives the ctrl+x screen (subagents/screen.py)
    """

    CSS_PATH = "styles.tcss"
    # Textual binds its command palette to ctrl+p with priority, which would
    # shadow the plain show_plan binding below and make the plan screen
    # unreachable by key. Move the palette off to ctrl+shift+p (distinct only
    # on terminals with the extended keyboard protocol — elsewhere the palette
    # simply has no key, which is the right trade: the plan screen is ours).
    COMMAND_PALETTE_BINDING = "ctrl+shift+p"
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
        self.status = StatusBar()
        self.stream = StreamRenderer(self)
        self.session = SessionView(self)
        # Recallable prompt history. Defaults to in-memory; the CLI passes a
        # persistent one so Up/Down recall prompts across restarts.
        self._history = history if history is not None else PromptHistory()
        self._turn_worker = None
        # Latch closing the window between "decided to start a turn" and the
        # exclusive worker actually existing. start_turn awaits a mount before it
        # can set _turn_worker, so without this a second submit landing in that gap
        # would pass the _turn_worker guard and start a *duplicate* exclusive
        # worker — which Textual resolves by silently cancelling the first, the
        # exact hazard turn_busy exists to prevent. Set before the first await,
        # cleared on every start_turn exit path.
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
        # Confirm-to-quit guard (see _QUIT_CONFIRM_WINDOW): timestamp of the last
        # unconfirmed quit attempt, or None if there isn't one outstanding.
        self._quit_warned_at: float | None = None
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off` and from the settings screen,
        # which is why it stays a plain App attribute rather than moving into the
        # ActivityMonitor that reads it.
        self.autonomous_wake = harness.autonomous_wake
        self.activity = ActivityMonitor(self)
        self.queue = QueueController(self)
        self.pickers = ModelPickers(self)
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
            on_subagent_thinking=self.stream.on_subagent_thinking,
            on_subagent_usage=self.stream.on_subagent_usage,
            on_cli_activity=self.stream.on_cli_activity,
            on_ttft=self.stream.on_ttft,
            on_mode_change=self._refresh_mode_display,
            on_tasks_changed=self.activity.on_tasks_changed,
            on_jobs_changed=self.activity.on_jobs_changed,
            on_compact=self.session.on_compact,
            on_compact_start=self.session.on_compact_start,
            on_notice=self.session.on_notice,
            on_rename=self.session.on_rename,
        )
        self._autocomplete: CommandAutocomplete | None = None
        # Full-bleed sub-agents screen (ctrl+x): its open/navigate/close lifecycle
        # and the per-frame repaint coalescing live in this collaborator.
        self.subagents = SubAgentsScreen(self)

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield QueueDisplay()
        yield self.status
        yield CompactNotice()
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
        self.status.mode = self.harness.deps.workspace.mode.value
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
        self.activity.render_tasks()  # reflect any checklist restored with the session
        self.activity.render_jobs()  # process-scoped jobs survive session switches
        self.queue.render()
        self._announce_session_defaults()
        # Seed vision capabilities in the background so the text-only-model
        # warning can fire even before the user opens the model picker.
        source = self.harness.model_source
        if source is not None:
            self.run_worker(
                self.pickers.refresh_vision_caps(source.list_models), exclusive=False
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
        # First-open trust prompt: bootstrap only sets trust_prompt when the
        # project ships a gated surface AND no decision (env/store) already
        # resolved it. Kicked off as its own worker (not awaited inline) so
        # on_mount itself isn't held hostage to the user answering the panel.
        if getattr(self.harness, "trust_prompt", None) is not None:
            self.run_worker(
                prompt_project_trust(self), group="trust", exit_on_error=False
            )

    def _announce_session_defaults(self) -> None:
        """One-line advisor/thinking status at session start, so a setting
        inherited from .env or restored with the session is visible without
        opening settings. An off/unset level stays silent — that's the default."""
        if self.harness.advisor_model_id is not None:
            self.append_log(
                NoticeMessage(f"Advisor: {self.harness.advisor_model_id} · /advisor")
            )
        level = self.harness.thinking_level_id
        if level is not None and level != "off":
            self.append_log(NoticeMessage(f"Thinking: {level} · /think"))

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
            except OSError:
                pass
        await self.jobs.cancel_all()
        await self.harness.session_end("exit")
        await self.harness.aclose()

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
        before ``start_turn`` has set ``_turn_worker``."""
        return self._turn_worker is not None or self._turn_starting

    def _refresh_mode_display(self) -> None:
        """Push the current mode into the status bar's ``mode`` reactive.

        Called from action_cycle_mode, the /mode command, and via the
        on_mode_change UIHooks callback so a tool that flips workspace.mode
        mid-turn (e.g. present_plan) can nudge the status bar to redraw.
        Runs on the event-loop thread: the exclusive turn worker is an asyncio
        task, so no call_from_thread marshalling is needed — Textual widget
        mutations from asyncio tasks are safe.
        """
        self.status.mode = self.harness.deps.workspace.mode.value

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

    # --- Turn lifecycle ---

    async def start_turn(
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
            self.activity.note_user_turn()
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
        /remember or /skill that injects its own prompt. Unlike start_turn it
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
        # Mirror start_turn's discipline: keep the spawn exception-safe. This path
        # has no awaits (so no concurrent submit can interleave, hence no
        # _turn_starting latch is needed), but resetting the stream or creating the
        # worker could still raise if Textual is mid-teardown. If it does, leave no
        # half-set busy state behind (_turn_worker stays/returns to None so
        # turn_busy doesn't wedge) and report failure rather than letting the
        # exception escape into the slash-command dispatcher.
        try:
            self.stream.current_assistant = None
            self._turn_worker = self.run_worker(self._run_turn(prompt), exclusive=True)
        except Exception as exc:  # noqa: BLE001 — a failed spawn must not wedge the UI
            self._turn_worker = None
            self.log.error("failed to start system turn")
            logger.warning("failed to start system turn: %s", exc, exc_info=True)
            self.append_log(
                NoticeMessage("Couldn't start the command — please try again.")
            )
            return False
        return True

    def mount_wake_turn(self) -> None:
        """The wake effect the ActivityMonitor's driver invokes: post the resume
        notice and spawn the digest-only turn worker. Mounted synchronously (we
        may be in a sync on_change callback), mirroring on_compact / on_rename."""
        self.append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)

    def action_cancel_turn(self) -> None:
        if self.status.busy and self._turn_worker is not None:
            self._turn_worker.cancel()

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
            self.activity.desktop_notify(
                "Turn complete", f"Finished in {elapsed}", "turn_complete"
            )
        except CancelledError:
            # User pressed escape; mount synchronously (we are unwinding) and
            # let the worker finish as cancelled.
            self.queue.paused = True
            self.append_log(ErrorMessage("turn cancelled"))
            # Settle anything still pending: a cancelled turn otherwise leaves its
            # tool rows and sub-agent cards "pending" forever, each holding a 10Hz
            # repaint timer and rendering a spinner for work that is already dead.
            self.stream.settle_pending("cancelled")
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            self.queue.paused = True
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            self.append_log(ErrorMessage(detail))
            self.activity.desktop_notify("Turn error", detail, "error")
            logger.warning("turn failed", exc_info=True)
            # Same leak as the cancel arm above: a turn that dies mid tool-call
            # (a provider 500, a malformed response) leaves that row/card
            # "pending" with a live 10Hz timer just as surely as an Esc does.
            self.stream.settle_pending(detail)
        finally:
            self._turn_worker = None
            self.status.set_busy(False)
            # Guard against an orphaned compaction notice if maybe_compact raised
            # between on_compact_start() and on_compact(). query_one is guarded
            # because this runs during teardown too, where the widget may already
            # be gone: a NoMatches here would skip after_turn() below (stranding
            # the queue and the wake chain) and, with no exit_on_error=False on
            # the turn worker, take the app down.
            with suppress(NoMatches):
                self.query_one(CompactNotice).compacting = False
            await self.queue.after_turn()  # drain next queued item, or wake on jobs

    # --- Queue actions (the Textual surface; QueueController does the work) ---

    async def action_run_queued(self) -> None:
        await self.queue.resume()

    def action_remove_queued(self, id: str) -> None:
        self.queue.remove(id)

    async def action_edit_queued(self, id: str) -> None:
        await self.queue.edit_in_prompt(id)

    # --- Quitting ---

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
        if self.queue:
            message = (
                f"{len(self.queue.items)} queued message(s) will be discarded. "
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

    def warn_typed_quit_discards(self) -> bool:
        """The /exit · /quit counterpart of ``_maybe_warn_pending_quit``. Returns
        True if the quit should be cancelled (a warning was just shown).

        Deliberately *not* the same guard as ctrl+c. That one always warns,
        because the risk it defends against is a stray keypress — which doesn't
        apply to six characters typed on purpose. What does still apply is the
        silent data loss: before this existed, /exit called app.exit() directly
        and threw away everything the user had queued without a word. So this
        warns only when there is actually something to discard, and over a window
        wide enough to re-type the command (_TYPED_QUIT_CONFIRM_WINDOW).

        Shares ``_quit_warned_at`` with the ctrl+c guard on purpose: a user who
        was just told what a quit would cost shouldn't be told twice for
        switching from the key to the command."""
        if not self.queue:
            return False
        now = time.monotonic()
        if (
            self._quit_warned_at is not None
            and now - self._quit_warned_at <= _TYPED_QUIT_CONFIRM_WINDOW
        ):
            return False
        self._quit_warned_at = now
        self.query_one("#log", VerticalScroll).mount(
            NoticeMessage(
                f"{len(self.queue.items)} queued message(s) will be discarded. "
                "Run /exit again to confirm."
            )
        )
        return True

    # --- Log helpers ---

    async def post_system(self, markdown: str) -> None:
        """Render a system/command message into the log (markdown)."""
        log = self.query_one("#log", VerticalScroll)
        msg = AssistantMessage()
        await log.mount(msg)
        self.stream.append_stream(msg, markdown)
        self.stream.flush_streams()  # one-shot system text: render it now, no tick wait

    def append_log(self, widget) -> None:
        """Mount a notice/error into the log, keeping the viewport pinned to the
        bottom only if it was already there. A user who scrolled up to read history
        isn't yanked back down, but a user following live still sees new messages
        (errors, steering echoes) scroll into view instead of landing off-screen."""
        log = self.query_one("#log", VerticalScroll)
        at_bottom = log.scroll_offset.y >= log.max_scroll_y
        log.mount(widget)
        if at_bottom:
            log.scroll_end(animate=False)

    # --- Session lifecycle (guards here; SessionView does the rebuild) ---

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd).
        Refused mid-turn — clearing would tear down the log the running turn is
        still streaming into and wipe history it is appending to."""
        if await self._refuse_if_session_busy("clear"):
            return
        await self.session.reset_conversation()

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk. Refused
        mid-turn — switching the active session out from under a running turn
        would race its history persist."""
        if await self._refuse_if_session_busy("start a new session"):
            return
        await self.session.start_new_session(name)

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off. Refused mid-turn
        for the same reason as /new — the running turn writes to the session it
        would be switched away from."""
        if await self._refuse_if_session_busy("switch sessions"):
            return
        await self.session.switch_to_session_id(session_id)

    async def _refuse_if_session_busy(self, what: str) -> bool:
        """True (with a notice posted) when ``what`` must not run right now.
        Both flows below tear down or rebind the session store, so both have to
        wait out a running turn *and* an in-flight compaction."""
        if self.turn_busy:
            await self.post_system(
                f"Can't {what} while a turn is running. Press Esc first."
            )
            return True
        if self.compact_busy:
            await self.post_system("Compaction in progress — wait for it to finish.")
            return True
        return False

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

    async def open_session_picker(self) -> None:
        """Open the session picker and let the user browse/filter/switch/delete
        saved sessions. Sessions are fetched synchronously up front (listing is
        a cheap header-only parse — see session/store.py's _header_fields), so
        unlike the model pickers there's no async fetch/loading state to manage.

        Uses the callback form of push_screen (not push_screen_wait) for the same
        reason ModelPickers.open_model does: /sessions dispatches from the command
        path, which is not a worker — push_screen_wait would raise NoActiveWorker
        there.
        """
        infos = self.harness.session.sessions()
        store = self.harness.session.store
        active = store.session_id if store is not None else None
        self.push_screen(SessionPickerModal(infos, active), self._on_session_chosen)

    async def _on_session_chosen(self, chosen: str | None) -> None:
        """Apply a session selected in the picker. Invoked by push_screen when the
        modal is dismissed; a None result (cancelled) is a no-op. Routes through
        switch_to_session_id above, so the mid-turn refusal guard applies here too."""
        if not chosen:
            return
        await self.switch_to_session_id(chosen)

    def on_session_picker_modal_deleted(self, message: SessionPickerModal.Deleted) -> None:
        """The picker already removed the row optimistically; this performs the
        actual on-disk teardown via the same SessionManager.delete used by
        `marim sessions delete` (interfaces/cli/sessions.py)."""
        manager = self.harness.session.manager
        if manager is not None:
            manager.delete(message.session_id)

    # --- Callbacks the harness reaches the user through (see bind_ui) ---

    async def _request_approval(self, call) -> DeferredToolApprovalResult | bool:
        self.activity.desktop_notify(
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
        self.activity.desktop_notify("Question from agent", prompt, "ask_user")
        return await run_panel(self, AskUserPanel(questions))

    async def _present_plan(self, summary, steps, choices):
        """Put the finished plan to the user as an inline card and return a
        PlanDecision (the chosen execution label, or "Keep planning" with revise-feedback).
        Inline panel, not a modal — the transcript stays scrollable; a cancelled turn
        removes the card via run_panel's finally. The plan's summary/steps already live
        on deps.plan (set by present_plan), so the pinned title and Ctrl+P overlay stay
        in sync regardless of the choice made here."""
        self.activity.desktop_notify("Plan ready", summary, "ask_user")
        # Refresh the TaskPanel title now that deps.plan is set.
        self.activity.render_tasks()
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
        # Re-derive the float offset from the prompt's *current* height every
        # time: the box grows with its content, so a menu positioned once (or by
        # a stylesheet constant) ends up covering a multi-line draft.
        self._autocomplete.position_above(self.query_one(PromptInput).box_height)
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

    # --- Submission routing ---

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
        reason = self.pickers.image_block_reason(event.attachments)
        if reason is not None:
            self.append_log(NoticeMessage(reason))
            return
        self.harness.steer(text, event.attachments)
        tag = f"  📎 {len(event.attachments)}" if event.attachments else ""
        self.append_log(NoticeMessage(f"↪ steering: {text}{tag}"))

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
        reason = self.pickers.image_block_reason(attachments)
        if reason is not None:
            self.append_log(NoticeMessage(reason))
            return
        if self.compact_busy:
            # Refuse (don't enqueue) so the turn isn't silently lost or run against
            # a session the compact worker is mid-summarize on. Symmetric with the
            # notice /compact posts when a turn is running.
            self.append_log(
                NoticeMessage("Compaction in progress — wait for it to finish.")
            )
            return
        if self.turn_busy:
            # turn_busy (not _turn_worker) so a submit landing in the start-up gap
            # is queued rather than racing a second exclusive worker.
            self.queue.enqueue(text, attachments)
            return
        self.queue.paused = False
        await self.start_turn(text, attachments)

    # --- `!` shell passthrough (pure helpers live in shell_passthrough.py) ---

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
            self.append_log(NoticeMessage(
                "Can't run a shell command while a turn is running. "
                "Press Esc first."
            ))
            return
        # group="shell-passthrough": Textual's WorkerManager cancels every worker
        # sharing a group when a new *exclusive* worker joins that group. The turn
        # worker (start_turn) runs exclusive=True in the default group, so leaving
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
            # exit_on_error=True (see the notification worker for the same
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
                self.append_log(NoticeMessage("sudo command cancelled"))
                return
        try:
            output = await run_passthrough(
                self.harness.deps.workspace.root, command, password
            )
        except OSError as exc:
            self.append_log(ErrorMessage(f"! {command} failed to start: {exc}"))
            return
        try:
            # Queue before rendering: if the render below fails, the model still
            # gets the output on the next turn even though the transcript never
            # showed it — losing the render is recoverable (the user can scroll
            # up or re-run), losing the model-context entry silently is worse.
            self.harness.add_shell_result(command, output)
            await self.post_system(format_transcript_block(command, output))
        except Exception as exc:  # keep the session alive on any render failure
            self.append_log(ErrorMessage(f"! {command}: {type(exc).__name__}: {exc}"))
            logger.warning("failed to render shell passthrough output", exc_info=True)
