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
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Footer, Header, Static

from ..agent import Harness
from ..compaction import estimate_tokens
from .approval import ApprovalModal
from .commands import dispatch
from .model_picker import ModelPickerModal
from .widgets import (
    AssistantMessage,
    ErrorMessage,
    NoticeMessage,
    PromptInput,
    ToolCallWidget,
    UserMessage,
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

def _human_tokens(n: int) -> str:
    """Compact token count: 950 -> '950', 1500 -> '1.5k', 100000 -> '100k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


_WELCOME = (
    "Type a message below to start, or `/help` for commands.\n\n"
    "- `enter` sends · `shift+enter` (or `ctrl+j`) inserts a newline\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `esc` cancels the running turn\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits"
)


class HarnessApp(App):
    CSS = """
    #log { height: 1fr; padding: 0 1; }
    PromptInput { height: 3; max-height: 10; border: none; padding: 0 1; }
    #status-bar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    .user-msg { color: $accent; text-style: bold; margin-top: 1; }
    .error-msg { color: $error; text-style: bold; margin: 1 0; }
    .notice-msg { color: $text-muted; text-style: italic; margin: 1 0; }
    #banner { color: $accent; text-style: bold; height: auto; margin: 1 0 1 0; }
    AssistantMessage { margin: 0 0 1 0; }
    ToolCallWidget { margin: 0 0 1 0; }
    """
    BINDINGS = [
        ("ctrl+t", "cycle_mode", "Cycle mode"),
        ("escape", "cancel_turn", "Cancel turn"),
    ]

    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness
        self.harness.deps.request_approval = self._request_approval
        self.harness.on_compact = self._on_compact
        self.harness.on_rename = self._on_rename
        self._current_assistant: AssistantMessage | None = None
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        self._busy = False
        self._turn_worker = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield Static(self._status_text(), id="status-bar")
        yield PromptInput()
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "marim-harness"
        log = self.query_one("#log", VerticalScroll)
        await log.mount(Static(_BANNER, id="banner", markup=False))
        intro = AssistantMessage()
        await log.mount(intro)
        if self.harness.history:
            n = len(self.harness.history)
            tokens = self.harness.total_tokens
            intro.append(
                f"**Resumed session** — {n} messages, {tokens} tokens restored."
            )
            await self._replay_history(log)
        else:
            intro.append(_WELCOME)
        log.scroll_end(animate=False)

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
        for message in self.harness.history:
            if isinstance(message, (ModelRequest, ModelResponse)):
                for part in message.parts:
                    if isinstance(part, UserPromptPart):
                        content = part.content
                        text = content if isinstance(content, str) else str(content)
                        await log.mount(UserMessage(text))
                    elif isinstance(part, TextPart):
                        if part.content:
                            msg = AssistantMessage()
                            await log.mount(msg)
                            msg.append(part.content)
                    elif isinstance(part, ToolCallPart):
                        widget = ToolCallWidget(part.tool_name, part.args_as_dict())
                        tool_widgets[part.tool_call_id] = widget
                        await log.mount(widget)
                    elif isinstance(part, ToolReturnPart):
                        widget = tool_widgets.get(part.tool_call_id)
                        if widget is not None:
                            widget.finish(str(part.content))

    def _status_text(self) -> str:
        cfg = getattr(self.harness, "model_label", "model")
        spent = getattr(self.harness, "total_tokens", 0)
        used = estimate_tokens(self.harness.history)
        max_ctx = getattr(self.harness, "max_context_tokens", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx = f"ctx {_human_tokens(used)}/{_human_tokens(max_ctx)} ({pct}%)"
        if pct >= 90:
            ctx = f"[red]{ctx}[/]"
        elif pct >= 75:
            ctx = f"[yellow]{ctx}[/]"
        name = getattr(self.harness, "session_name", None)
        prefix = f"{name} · " if name else ""
        base = (
            f"{prefix}{self.harness.deps.mode.value} · {cfg} · {ctx} · "
            f"{_human_tokens(spent)} tokens"
        )
        return f"{base} · working…" if self._busy else base

    def _refresh_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._refresh_status()

    def action_cycle_mode(self) -> None:
        self.harness.deps.mode = self.harness.deps.mode.cycle()
        self._refresh_status()

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
        log.scroll_end(animate=False)
        self._refresh_status()  # context gauge shrinks immediately

    def _on_rename(self, old: str, new: str) -> None:
        """Note an automatic session title in the log. Called synchronously from
        run_turn; mount without awaiting."""
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage(f"session renamed: {new}"))
        log.scroll_end(animate=False)
        self._refresh_status()

    async def post_system(self, markdown: str) -> None:
        """Render a system/command message into the log (markdown)."""
        log = self.query_one("#log", VerticalScroll)
        msg = AssistantMessage()
        await log.mount(msg)
        msg.append(markdown)
        log.scroll_end(animate=False)

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
        intro.append(note)
        if self.harness.history:
            await self._replay_history(log)
        self._refresh_status()
        log.scroll_end(animate=False)

    async def reset_conversation(self) -> None:
        """Wipe the conversation and re-show the welcome screen (the /clear cmd)."""
        self.harness.reset()
        await self._render_session(_WELCOME)

    async def start_new_session(self, name: str | None = None) -> None:
        """Begin a fresh named session, leaving existing ones on disk."""
        self.harness.new_session(name)
        label = self.harness.session_name or "new session"
        await self._render_session(f"**New session** — `{label}`.")

    async def switch_to_session_id(self, session_id: str) -> None:
        """Load an existing session and show where it left off."""
        n = self.harness.switch_session(session_id)
        label = self.harness.session_name or session_id
        await self._render_session(
            f"**Switched to** `{label}` — {n} messages restored."
        )

    async def open_model_picker(self) -> None:
        """Fetch the provider's catalog and let the user pick a model, applying
        the choice to the harness. Degrades to free-text when no catalog loads.

        Uses the callback form of push_screen (not push_screen_wait) so it works
        when called straight from the command-dispatch path, which is not a
        worker — push_screen_wait would raise NoActiveWorker there.
        """
        source = self.harness.model_source
        if source is None:
            await self.post_system("Model switching isn't available here.")
            return
        entries = await source.list_models()
        if not entries and not source.is_local:
            await self.post_system(
                "Couldn't fetch the model catalog — type a model id to set it directly."
            )
        self.push_screen(
            ModelPickerModal(
                entries,
                allow_free_text=source.is_local or not entries,
                current=self.harness.model_id,
            ),
            self._on_model_chosen,
        )

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

    async def _request_approval(self, call) -> object:
        approved = await self.push_screen_wait(
            ApprovalModal(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")

    async def on_prompt_input_submitted(self, event: PromptInput.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        self.query_one(PromptInput).text = ""
        if text.startswith("/"):
            await dispatch(self, text)
            return
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self._current_assistant = None
        self._turn_worker = self.run_worker(self._run_turn(text), exclusive=True)

    async def _run_turn(self, text: str) -> None:
        self._set_busy(True)
        log = self.query_one("#log", VerticalScroll)
        try:
            await self.harness.run_turn(text, event_stream_handler=self._on_events)
        except CancelledError:
            # User pressed escape; mount synchronously (we are unwinding) and
            # let the worker finish as cancelled.
            log.mount(ErrorMessage("turn cancelled"))
            log.scroll_end(animate=False)
            raise
        except Exception as exc:  # keep the session alive on any turn failure
            await log.mount(ErrorMessage(f"{type(exc).__name__}: {exc}"))
            log.scroll_end(animate=False)
        finally:
            self._turn_worker = None
            self._set_busy(False)

    async def _on_events(self, ctx, events) -> None:
        log = self.query_one("#log", VerticalScroll)
        async for event in events:
            if isinstance(event, PartStartEvent) and isinstance(event.part, TextPart):
                self._current_assistant = AssistantMessage()
                await log.mount(self._current_assistant)
                if event.part.content:
                    self._current_assistant.append(event.part.content)
            elif isinstance(event, PartDeltaEvent) and isinstance(
                event.delta, TextPartDelta
            ):
                if self._current_assistant is not None:
                    self._current_assistant.append(event.delta.content_delta or "")
            elif isinstance(event, FunctionToolCallEvent):
                widget = ToolCallWidget(
                    event.part.tool_name, event.part.args_as_dict()
                )
                self._tool_widgets[event.part.tool_call_id] = widget
                await log.mount(widget)
            elif isinstance(event, FunctionToolResultEvent):
                widget = self._tool_widgets.get(event.tool_call_id)
                if widget is not None:
                    widget.finish(str(getattr(event.part, "content", "")))
            log.scroll_end(animate=False)
