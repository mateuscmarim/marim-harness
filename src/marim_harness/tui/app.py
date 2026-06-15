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
from textual.widgets import Input, Static

from ..agent import Harness
from .approval import ApprovalModal
from .widgets import AssistantMessage, ToolCallWidget, UserMessage


class HarnessApp(App):
    CSS = """
    #log { height: 1fr; }
    #status-bar { height: 1; dock: bottom; background: $panel; }
    Input { dock: bottom; }
    """
    BINDINGS = [("ctrl+t", "cycle_mode", "Cycle mode")]

    def __init__(self, harness: Harness) -> None:
        super().__init__()
        self.harness = harness
        self.harness.deps.request_approval = self._request_approval
        self._current_assistant: AssistantMessage | None = None
        self._tool_widgets: dict[str, ToolCallWidget] = {}

    def compose(self) -> ComposeResult:
        yield VerticalScroll(id="log")
        yield Static(self._status_text(), id="status-bar")
        yield Input(placeholder="type a message…")

    def _status_text(self) -> str:
        cfg = getattr(self.harness, "model_label", "model")
        return f"{self.harness.deps.mode.value} · {cfg}"

    def _refresh_status(self) -> None:
        self.query_one("#status-bar", Static).update(self._status_text())

    def action_cycle_mode(self) -> None:
        self.harness.deps.mode = self.harness.deps.mode.cycle()
        self._refresh_status()

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
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self._current_assistant = None
        self.run_worker(self._run_turn(text), exclusive=True)

    async def _run_turn(self, text: str) -> None:
        await self.harness.run_turn(text, event_stream_handler=self._on_events)
        self._refresh_status()

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
