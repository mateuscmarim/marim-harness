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
from textual.widgets import Footer, Header, Input, Static

from ..agent import Harness
from .approval import ApprovalModal
from .widgets import (
    AssistantMessage,
    ErrorMessage,
    NoticeMessage,
    ToolCallWidget,
    UserMessage,
)

_WELCOME = (
    "Welcome to **marim-harness**. Type a message below to start.\n\n"
    "- `ctrl+t` cycles the approval mode (ask → auto → plan)\n"
    "- `/exit` (or `/quit`, `ctrl+c`) quits"
)


class HarnessApp(App):
    CSS = """
    #log { height: 1fr; padding: 0 1; }
    #status-bar { height: 1; background: $panel; color: $text-muted; padding: 0 1; }
    .user-msg { color: $accent; text-style: bold; margin-top: 1; }
    .error-msg { color: $error; text-style: bold; margin: 1 0; }
    .notice-msg { color: $text-muted; text-style: italic; margin: 1 0; }
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
        self._current_assistant: AssistantMessage | None = None
        self._tool_widgets: dict[str, ToolCallWidget] = {}
        self._busy = False
        self._turn_worker = None

    def compose(self) -> ComposeResult:
        yield Header(show_clock=False)
        yield VerticalScroll(id="log")
        yield Static(self._status_text(), id="status-bar")
        yield Input(placeholder="type a message…")
        yield Footer()

    async def on_mount(self) -> None:
        self.title = "marim-harness"
        log = self.query_one("#log", VerticalScroll)
        banner = AssistantMessage()
        await log.mount(banner)
        if self.harness.history:
            n = len(self.harness.history)
            tokens = self.harness.total_tokens
            banner.append(
                f"**Resumed session** — {n} messages, {tokens} tokens restored."
            )
            await self._replay_history(log)
        else:
            banner.append(_WELCOME)
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
        tokens = getattr(self.harness, "total_tokens", 0)
        base = f"{self.harness.deps.mode.value} · {cfg} · {tokens} tokens"
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

    async def _request_approval(self, call) -> object:
        approved = await self.push_screen_wait(
            ApprovalModal(call.tool_name, call.args_as_dict())
        )
        return True if approved else ToolDenied("denied by user")

    async def on_input_submitted(self, event: Input.Submitted) -> None:
        text = event.value.strip()
        if not text:
            return
        event.input.value = ""
        if text in ("/exit", "/quit"):
            self.exit()
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
