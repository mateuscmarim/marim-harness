from asyncio import CancelledError

from pydantic_ai import ToolDenied
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
)
from pydantic_ai.tools import DeferredToolApprovalResult
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.content import Content
from textual.css.query import NoMatches
from textual.widget import Widget
from textual.widgets import Footer, Header, Static

from ...agent import Harness, strip_turn_context
from ...compaction import estimate_tokens
from ...history import PromptHistory
from ...prefs import load_theme, save_theme
from ...usage import resolve_cost
from .approval import ApprovalModal
from .ask_user import AskUserModal
from .commands import dispatch
from .model_picker import ModelPickerModal
from .settings import SettingsModal
from .themes import MARIM_THEMES
from .widgets import (
    AssistantMessage,
    ErrorMessage,
    JobPanel,
    NoticeMessage,
    PromptInput,
    SubAgentWidget,
    TaskPanel,
    ToolCallWidget,
    ToolGroupWidget,
    UserMessage,
)
from .widgets import format_cost as _format_cost
from .widgets import format_token_split as _format_token_split
from .widgets import human_tokens as _human_tokens

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


class _StreamSink:
    """Where one event stream's widgets land and how its run-state is read/written.

    Routing a streamed turn is identical whether the events come from the
    top-level agent or a nested sub-agent — the only things that differ are the
    mount container, where this stream's run-state and current assistant message
    live, the title bookkeeping, and whether a tool call gets intercepted (the
    spawn_agent special case). A sink captures exactly those, so one dispatch core
    (:meth:`HarnessApp._dispatch_stream_event`) serves both. Hooks default to
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

    def on_text(self) -> None:
        """Called when the stream starts a text part (title status, sub only)."""

    def on_tool(self, tool_name: str) -> None:
        """Called when the stream makes a tool call (title status, sub only)."""

    async def intercept_tool(self, event, args: dict) -> bool:
        """Give the scope first refusal on a tool call; return True to claim it and
        skip the default ToolCallWidget path. Default: never intercepts."""
        return False

    def on_result(self, event) -> None:
        """Called after a tool result is rendered (cleanup hook)."""


class _TopLevelSink(_StreamSink):
    """The top-level turn stream: mounts into the main log, keeps run-state and the
    current assistant on the app's scalar fields, and claims foreground spawn_agent
    calls so they render as a live SubAgentWidget instead of a generic tool."""

    def __init__(self, app: "HarnessApp", container) -> None:
        self._app = app
        self.container = container

    def get_run(self) -> tuple:
        return self._app._tool_group, self._app._solo_tool

    def set_run(self, group, solo) -> None:
        self._app._tool_group = group
        self._app._solo_tool = solo

    def get_assistant(self):
        return self._app._current_assistant

    def set_assistant(self, msg) -> None:
        self._app._current_assistant = msg

    async def intercept_tool(self, event, args: dict) -> bool:
        # A background spawn returns a job id immediately and doesn't stream its
        # steps, so it falls through to a plain tool widget; only a foreground
        # spawn gets the live SubAgentWidget (and is mounted standalone, breaking
        # the run so it isn't buried in a tool group).
        if event.part.tool_name == "spawn_agent" and not args.get("background"):
            widget = self._app._mount_spawn_widget(args)
            self._app._tool_widgets[event.part.tool_call_id] = widget
            self.set_run(None, None)
            await self.container.mount(widget)
            return True
        return False

    def on_result(self, event) -> None:
        # A foreground spawn's stream_id is its tool_call_id; drop its sub-agent
        # assistant entry once the spawn returns.
        self._app._sub_assistants.pop(event.tool_call_id, None)


class _SubAgentSink(_StreamSink):
    """A nested sub-agent stream: mounts into its SubAgentWidget body, keeps
    run-state and the current assistant in per-stream dicts keyed by ``stream_id``,
    and pushes live text/tool activity into the (collapsed) widget title."""

    def __init__(self, app: "HarnessApp", parent: SubAgentWidget, stream_id: str) -> None:
        self._app = app
        self._parent = parent
        self._sid = stream_id
        self.container = parent.body

    def get_run(self) -> tuple:
        return (self._app._sub_tool_groups.get(self._sid),
                self._app._sub_solo_tools.get(self._sid))

    def set_run(self, group, solo) -> None:
        self._app._sub_tool_groups[self._sid] = group
        self._app._sub_solo_tools[self._sid] = solo

    def get_assistant(self):
        return self._app._sub_assistants.get(self._sid)

    def set_assistant(self, msg) -> None:
        self._app._sub_assistants[self._sid] = msg

    def on_text(self) -> None:
        self._parent.note_text()

    def on_tool(self, tool_name: str) -> None:
        self._parent.note_tool(tool_name)


class HarnessApp(App):
    CSS_PATH = "styles.tcss"
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("escape", "cancel_turn", "Cancel turn"),
    ]

    def __init__(self, harness: Harness, history: PromptHistory | None = None) -> None:
        super().__init__()
        self.harness = harness
        # Recallable prompt history. Defaults to in-memory; the CLI passes a
        # persistent one so Up/Down recall prompts across restarts.
        self._history = history if history is not None else PromptHistory()
        self.harness.deps.request_approval = self._request_approval
        self.harness.deps.ask_user = self._ask_user
        self.harness.deps.tasks.on_change = self._on_tasks_changed
        self.harness.deps.jobs.on_change = self._on_jobs_changed
        self.harness.deps.on_subagent_event = self._on_subagent_event
        self.harness.session.on_compact = self._on_compact
        self.harness.session.on_rename = self._on_rename
        self._current_assistant: AssistantMessage | None = None
        self._tool_widgets: dict[str, ToolCallWidget | SubAgentWidget] = {}
        # State of the current run of consecutive top-level tool calls. A run only
        # becomes a group once it holds 2+ calls — a lone call stays a bare
        # ToolCallWidget (_solo_tool), since wrapping one tool adds a redundant
        # header and an extra click. The second call promotes the pair into a
        # group (_tool_group). Both reset (to None) when the run breaks (assistant
        # text, a spawn, or a new run). Sub-agent streams keep their own run state
        # keyed by the owning spawn's tool_call_id.
        self._tool_group: ToolGroupWidget | None = None
        self._solo_tool: ToolCallWidget | None = None
        self._sub_tool_groups: dict[str, ToolGroupWidget | None] = {}
        self._sub_solo_tools: dict[str, ToolCallWidget | None] = {}
        # Per-stream live assistant text for spawned sub-agents, keyed by the
        # spawn_agent tool_call_id that owns the nested stream.
        self._sub_assistants: dict[str, AssistantMessage] = {}
        # Streams that buffered deltas since the last flush tick. Draining only
        # these (instead of walking the whole message tree every frame) keeps the
        # tick O(active streams), not O(every message ever shown).
        self._dirty_streams: set[AssistantMessage] = set()
        self._vision_caps: dict[str, bool | None] = {}
        self._busy = False
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
        # The current turn's in-flight token total, read off ctx.usage as events
        # stream. Session usage only commits when a run finishes, so this is added
        # on top to make the counter climb live; reset to 0 once the turn ends
        # (and the run is folded into session usage) so it's never counted twice.
        self._live_run_tokens = 0

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield JobPanel()
        yield TaskPanel()
        yield Static(self._status_text(), id="status-bar")
        yield PromptInput(history=self._history)
        yield Footer()

    async def on_mount(self) -> None:
        for theme in MARIM_THEMES:
            self.register_theme(theme)
        self.theme = load_theme()
        self.title = "marim-harness"
        self.sub_title = str(self.harness.deps.workspace_root)
        log = self.query_one("#log", VerticalScroll)
        await log.mount(Static(_BANNER, id="banner", markup=False))
        intro = AssistantMessage()
        await log.mount(intro)
        if self.harness.session.history:
            n = len(self.harness.session.history)
            tokens = self.harness.session.total_tokens
            self._append_stream(
                intro,
                f"**Resumed session** — {n} messages, {tokens} tokens restored.",
            )
            await self._replay_history(log)
        else:
            self._append_stream(intro, _WELCOME)
        self._flush_streams()  # render the static intro/replay before first paint
        # Keep the log pinned to the bottom as content streams in. Textual's anchor
        # re-pins to the true bottom during layout (so it can't drift behind a
        # burst of text), auto-releases when the user scrolls up to read, and
        # auto-re-anchors when they scroll back down.
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
        self.set_interval(_STREAM_FLUSH_INTERVAL, self._flush_streams)
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
        await self.harness.deps.jobs.cancel_all()
        await self.harness.session_end("exit")
        await self.harness.aclose()

    async def _replay_history(self, log: VerticalScroll) -> None:
        """Re-render a restored conversation into the log so a resumed session
        looks like where you left off."""
        from pydantic_ai.messages import (
            ModelRequest,
            ModelResponse,
            TextPart,
            ToolCallPart,
            ToolReturnPart,
            UserPromptPart,
        )

        tool_widgets: dict[str, ToolCallWidget] = {}
        # The current run of consecutive tool calls during replay, mirroring the
        # live path so a resumed session groups bursts the same way: a lone call
        # stays bare, a burst folds into a group.
        group: ToolGroupWidget | None = None
        solo: ToolCallWidget | None = None
        for message in self.harness.session.history:
            if isinstance(message, (ModelRequest, ModelResponse)):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        group = None
                        solo = None
                        content = part.content
                        text = content if isinstance(content, str) else str(content)
                        # Drop any turn-context envelope (job digests, hook
                        # output, error notes) so the log shows only what the
                        # user typed — as the live path already does.
                        await log.mount(UserMessage(strip_turn_context(text)))
                    elif isinstance(part, TextPart):
                        if part.content:
                            group = None
                            solo = None
                            msg = AssistantMessage()
                            await log.mount(msg)
                            self._append_stream(msg, part.content)
                    elif isinstance(part, ToolCallPart):
                        widget = ToolCallWidget(part.tool_name, part.args_as_dict())
                        tool_widgets[part.tool_call_id] = widget
                        group, solo = await self._add_tool_to_run(
                            widget, log, group, solo
                        )
                    elif isinstance(part, ToolReturnPart):
                        widget = tool_widgets.get(part.tool_call_id)
                        if widget is not None:
                            widget.finish(str(part.content))

    def _status_text(self) -> Content:
        cfg = getattr(self.harness, "model_label", "model")
        used = estimate_tokens(self.harness.session.history)
        max_ctx = getattr(self.harness.session, "max_context_tokens", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {_human_tokens(used)}/{_human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        # The committed in/cached/out split, then the current run's in-flight
        # tokens as a live +N delta (they aren't split until the turn commits),
        # then spend — billed when the provider reports it, else estimated.
        tokens_text = _format_token_split(self.harness.session.usage)
        if self._live_run_tokens:
            tokens_text += f" +{_human_tokens(self._live_run_tokens)}"
        cost, _ = resolve_cost(self.harness.session.usage, self.harness.model_id)
        if cost is not None:
            tokens_text += f" · {_format_cost(cost)}"
        mode = self.harness.deps.mode.value
        name = getattr(self.harness.session, "session_name", None)
        # session_name is model-generated and untrusted; render it as a literal
        # styled segment via assemble so a stray bracket sequence (e.g. `[edit(`)
        # is never parsed as Textual markup — which would crash the status bar.
        head = (
            Content.assemble((name, "b $accent"), " · ", mode) if name else Content(mode)
        )
        fields = [
            head,
            Content(cfg),
            Content.assemble((ctx_text, ctx_style)) if ctx_style else Content(ctx_text),
            Content(tokens_text),
        ]
        if self._busy:
            fields.append(Content("working…"))
        return Content.from_markup(" [dim]·[/] ").join(fields)

    def _refresh_status(self) -> None:
        try:
            bar = self.query_one("#status-bar", Static)
        except NoMatches:
            # The status bar is gone — the app is tearing down (e.g. /exit fired
            # mid-turn) and a worker's finally block is still firing. Nothing to
            # update; quietly skip.
            return
        bar.update(self._status_text())

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        if not busy:
            # The finished run is now folded into session usage by run_turn; drop
            # the in-flight tally so it isn't added on top a second time.
            self._live_run_tokens = 0
        self._refresh_status()

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
        self._maybe_wake()

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
        self._refresh_status()

    def watch_theme(self, theme: str) -> None:
        """Persist the active theme so it's the startup theme next run. Only the
        marim themes are saved; Textual may set built-in defaults during init,
        which save_theme ignores."""
        save_theme(theme)

    def action_cancel_turn(self) -> None:
        if self._busy and self._turn_worker is not None:
            self._turn_worker.cancel()

    def _on_compact(self, before: int, after: int) -> None:
        """Note in the log when history was trimmed to stay under the token budget.
        Called synchronously from run_turn; mount without awaiting."""
        log = self.query_one("#log", VerticalScroll)
        log.mount(
            NoticeMessage(f"compacted history: {before} → {after} messages")
        )
        self._refresh_status()  # context gauge shrinks immediately

    def _on_rename(self, old: str, new: str) -> None:
        """Note an automatic session title in the log. Called synchronously from
        run_turn; mount without awaiting."""
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"session renamed: {new}"))
        self._refresh_status()

    async def post_system(self, markdown: str) -> None:
        """Render a system/command message into the log (markdown)."""
        log = self.query_one("#log", VerticalScroll)
        msg = AssistantMessage()
        await log.mount(msg)
        self._append_stream(msg, markdown)
        self._flush_streams()  # one-shot system text: render it now, no tick wait

    async def _render_session(self, note: str) -> None:
        """Rebuild the log for a fresh view of the active session: banner, an
        intro note, then a replay of any restored history."""
        self._current_assistant = None
        self._tool_widgets.clear()
        log = self.query_one("#log", VerticalScroll)
        await log.remove_children()
        await log.mount(Static(_BANNER, id="banner", markup=False))
        intro = AssistantMessage()
        await log.mount(intro)
        self._append_stream(intro, note)
        if self.harness.session.history:
            await self._replay_history(log)
        self._flush_streams()  # render the rebuilt log before first paint
        log.anchor()  # re-pin to the bottom for the freshly loaded session
        self._refresh_status()
        self._render_tasks()
        self._render_jobs()  # jobs are process-scoped, not per-session

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd)."""
        self.harness.reset()
        await self.harness.session_start("clear")
        await self._render_session(_WELCOME)

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk."""
        self.harness.new_session(name)
        await self.harness.session_start("startup")
        label = self.harness.session.session_name or "new session"
        await self._render_session(f"**New session** — `{label}`.")

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off."""
        n = self.harness.switch_session(session_id)
        await self.harness.session_start("resume")
        label = self.harness.session.session_name or session_id
        await self._render_session(
            f"**Switched to** `{label}` — {n} messages restored."
        )

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
        self._refresh_status()
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"model: {self.harness.model_label}"))
        log.scroll_end(animate=False)

    def _append_stream(self, widget: AssistantMessage, delta: str) -> None:
        """Buffer a streamed delta into ``widget`` and mark it for the next flush
        tick. Funnelling every append through here is what lets the tick render
        only the streams that actually changed."""
        widget.append(delta)
        self._dirty_streams.add(widget)

    def _flush_streams(self) -> None:
        """Render every AssistantMessage that buffered deltas since the last tick —
        top-level and nested sub-agent streams alike. Coalescing the markdown parses
        here is the streaming debounce; the log's scroll anchor keeps the freshly
        grown content pinned to the bottom. Draining the dirty set (rather than
        walking the whole message tree) keeps the tick proportional to the number
        of live streams."""
        dirty, self._dirty_streams = self._dirty_streams, set()
        for m in dirty:
            m.flush()
        # Piggyback on the same per-frame tick to repaint the status bar while a
        # turn is running, so the live token counter advances as the run streams.
        if self._busy:
            self._refresh_status()

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
        approved = await self.push_screen_wait(
            ApprovalModal(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")

    async def _ask_user(self, questions):
        """Put a structured question to the user and return their {header:
        answer} mapping, or None if they dismissed it. Runs inside the turn
        worker, so push_screen_wait is valid (same as _request_approval)."""
        return await self.push_screen_wait(AskUserModal(questions))

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
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
        self._current_assistant = None
        self._auto_turn_depth = 0  # a user turn breaks any autonomous-wake chain
        self._turn_worker = self.run_worker(
            self._run_turn(text, event.attachments), exclusive=True
        )

    async def _run_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        self._set_busy(True)
        log = self.query_one("#log", VerticalScroll)
        try:
            await self.harness.run_turn(
                text, event_stream_handler=self._on_events, attachments=attachments
            )
        except CancelledError:
            # User pressed escape; mount synchronously (we are unwinding) and
            # let the worker finish as cancelled.
            log.mount(ErrorMessage("turn cancelled"))
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            await log.mount(ErrorMessage(f"{type(exc).__name__}: {exc}"))
        finally:
            self._turn_worker = None
            self._set_busy(False)
            self._maybe_wake()  # a job that finished mid-turn drains now

    async def _add_tool_to_run(
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

    def _mount_spawn_widget(self, args: dict):
        """Build the widget for a foreground spawn_agent. When another sub-agent
        is already running, this is a fan-out — collapse every sibling (and this
        one) to a live one-line status so the log stays legible; a lone spawn is
        left expanded."""
        widget = SubAgentWidget(
            str(args.get("type", "")), str(args.get("task", ""))
        )
        live = [
            w for w in self._tool_widgets.values()
            if isinstance(w, SubAgentWidget) and w.status == "pending"
        ]
        if live:
            widget.collapsed = True
            for sibling in live:
                sibling.collapsed = True
        return widget

    async def _on_events(self, ctx, events) -> None:
        # Fresh run: clear any in-flight tally from a prior approval round so the
        # next round's usage replaces it rather than stacking (each agent.run gets
        # its own ctx.usage, cumulative for that run).
        self._live_run_tokens = 0
        # A new run starts a fresh run of consecutive tool calls.
        self._tool_group = None
        self._solo_tool = None
        sink = _TopLevelSink(self, self.query_one("#log", VerticalScroll))
        async for event in events:
            # ctx.usage carries the run's live running total (ctx is None in some
            # unit tests); fold it into the status counter via the flush tick.
            self._live_run_tokens = (
                getattr(getattr(ctx, "usage", None), "total_tokens", 0) or 0
            )
            await self._dispatch_stream_event(event, sink)

    async def _on_subagent_event(
        self, stream_id: str, event, usage=None
    ) -> None:
        """Route a spawned sub-agent's own stream into the SubAgentWidget that owns
        it. Shares _dispatch_stream_event with the top-level handler, but through a
        sub-agent sink that mounts into the widget body and tracks per-stream state.
        ``usage`` is the run's live RunUsage (or None): its total + cost ride in the
        (collapsed) title and the full cache split lands in the expanded body. Fired
        on the app's event loop, so direct widget mutation is safe and parallel
        streams stay race-free by stream_id."""
        parent = self._tool_widgets.get(stream_id)
        if not isinstance(parent, SubAgentWidget):
            return
        if usage is not None and usage.total_tokens:
            cost, _ = resolve_cost(usage, self.harness.model_id)
            cost_text = _format_cost(cost) if cost is not None else None
            parent.set_usage(usage.total_tokens, cost_text, _format_token_split(usage))
        await self._dispatch_stream_event(event, _SubAgentSink(self, parent, stream_id))

    async def _dispatch_stream_event(self, event, sink: _StreamSink) -> None:
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
                self._append_stream(msg, event.part.content)
        elif isinstance(event, PartDeltaEvent) and isinstance(
            event.delta, TextPartDelta
        ):
            msg = sink.get_assistant()
            if msg is not None:
                self._append_stream(msg, event.delta.content_delta or "")
        elif isinstance(event, FunctionToolCallEvent):
            # A gated tool re-emits its call event on the post-approval execution
            # pass; reuse the widget already mounted for this id rather than
            # mounting an orphaned duplicate.
            if event.part.tool_call_id in self._tool_widgets:
                return
            args = event.part.args_as_dict()
            if await sink.intercept_tool(event, args):
                return
            sink.on_tool(event.part.tool_name)  # live title status
            widget = ToolCallWidget(event.part.tool_name, args)
            self._tool_widgets[event.part.tool_call_id] = widget
            group, solo = sink.get_run()
            group, solo = await self._add_tool_to_run(
                widget, sink.container, group, solo
            )
            # Keep the run state in sync; a None value just means "no open group /
            # no lone call" for this stream.
            sink.set_run(group, solo)
        elif isinstance(event, FunctionToolResultEvent):
            widget = self._tool_widgets.get(event.tool_call_id)
            if widget is not None:
                widget.finish(str(getattr(event.part, "content", "")))
            sink.on_result(event)
