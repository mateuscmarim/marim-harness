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
from .widgets import (
    AssistantMessage,
    CommandAutocomplete,
    ErrorMessage,
    JobPanel,
    NoticeMessage,
    PromptInput,
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
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits"
)


class HarnessApp(App):
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("ctrl+o", "toggle_outputs", "Show all output"),
        ("escape", "cancel_turn", "Cancel turn"),
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
        self.harness.deps.request_approval = self._request_approval
        self.harness.deps.ask_user = self._ask_user
        self.harness.deps.tasks.on_change = self._on_tasks_changed
        self.harness.deps.jobs.on_change = self._on_jobs_changed
        self.harness.deps.on_subagent_event = self.stream.on_subagent_event
        self.harness.session.on_compact = self._on_compact
        self.harness.session.on_compact_start = self._on_compact_start
        self.harness.session.on_rename = self.session.on_rename
        self._compacting_notice: NoticeMessage | None = None
        self._vision_caps: dict[str, bool | None] = {}
        self._turn_worker = None
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off`.
        self.autonomous_wake = harness.autonomous_wake
        self._wake_depth_cap = harness.wake_depth_cap
        # Consecutive autonomous turns since the last user turn; reset on any
        # user-initiated turn. Bounds wake→spawn→wake chains.
        self._auto_turn_depth = 0
        # Ids of finished (done/failed) jobs already desktop-notified, so each
        # completion pings exactly once, independent of the autonomous-wake path.
        self._notified_jobs: set[str] = set()
        self._autocomplete: CommandAutocomplete | None = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield Static(self.status.status_text(), id="status-bar")
        yield CommandAutocomplete(id="cmd-autocomplete")
        yield PromptInput(history=self._history)
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
        self._render_tasks()  # reflect any checklist restored with the session
        self._render_jobs()  # process-scoped jobs survive session switches
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
        # Ensure the controller's segment timer is running even on fresh sessions
        # (resume() sets it for resumed sessions, but fresh starts skip resume).
        if self.harness.session._segment_start == 0.0:
            self.harness.session._segment_start = time.monotonic()
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
        # Persist session duration before tearing down.
        session = self.harness.session
        elapsed = (time.monotonic() - session._segment_start) if session._segment_start else 0.0
        session.duration_seconds += elapsed
        session.persist()
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
        self._render_jobs()
        self._notify_finished_jobs()
        self._maybe_wake()

    def _notify(self, title: str, body: str, event_type: str) -> None:
        """Fire a desktop notification if one is wired on deps. Best-effort —
        the notifier itself swallows all errors, so this is a safe no-op when
        notifications are off or the platform lacks a daemon."""
        notifier = self.harness.deps.notifier
        if notifier is not None:
            notifier.send(title, body, event_type)

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
        if not self.autonomous_wake:
            return
        if self._turn_worker is not None:
            return  # a turn is running; the digest drains on the next turn
        if self._auto_turn_depth >= self._wake_depth_cap:
            return  # loop guard: wait for the user
        if not self.harness.deps.jobs.has_finished_pending():
            return  # nothing finished -> no empty turn
        self._auto_turn_depth += 1
        # Mounted synchronously (we may be in a sync on_change callback), mirroring
        # _on_compact / _on_rename.
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)

    def action_cycle_mode(self) -> None:
        self.harness.deps.mode = self.harness.deps.mode.cycle()
        self.status.refresh_status()

    def action_toggle_outputs(self) -> None:
        """Ctrl+O: reveal every tool output in full (expand groups, uncap edit
        diffs), or restore the default view on a second press."""
        self.stream.toggle_reveal_all()

    def watch_theme(self, theme: str) -> None:
        """Persist the active theme so it's the startup theme next run. Only the
        marim themes are saved; Textual may set built-in defaults during init,
        which save_theme ignores."""
        save_theme(theme)

    def action_cancel_turn(self) -> None:
        if self.status.busy and self._turn_worker is not None:
            self._turn_worker.cancel()

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
        """Wipe the conversation and re-show the welcome screen (the /clear cmd)."""
        await self.session.reset_conversation()

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk."""
        await self.session.start_new_session(name)

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off."""
        await self.session.switch_to_session_id(session_id)

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
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"model: {self.harness.model_label}"))
        log.scroll_end(animate=False)

    def _image_block_reason(self, attachments) -> str | None:
        """A warning to show instead of submitting, or None to proceed. Only a
        positive text-only capability blocks; unknown always proceeds."""
        if not attachments:
            return None
        model_id = self.harness.model_id
        if model_id is not None and self._vision_caps.get(model_id) is False:
            return (f"{model_id} can't read images — "
                    "switch to a vision model (Ctrl+P) or remove the image.")
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
            log = self.query_one("#log", VerticalScroll)
            await log.mount(NoticeMessage(reason))
            return
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self.stream.current_assistant = None
        self._auto_turn_depth = 0  # a user turn breaks any autonomous-wake chain
        self._turn_worker = self.run_worker(
            self._run_turn(text, event.attachments), exclusive=True
        )

    async def _run_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        self.status.turn_start = time.monotonic()
        self.status.set_busy(True)
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
            log.mount(ErrorMessage("turn cancelled"))
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            detail = format_provider_error(exc) or f"{type(exc).__name__}: {exc}"
            await log.mount(ErrorMessage(detail))
            self._notify("Turn error", detail, "error")
        finally:
            self._turn_worker = None
            self.status.set_busy(False)
            self._maybe_wake()  # a job that finished mid-turn drains now
