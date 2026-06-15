from textual.app import ComposeResult
from textual.widgets import Collapsible, Static


class ToolCallWidget(Collapsible):
    """A single tool call: collapsed shows a summary line; expanded shows
    args and result."""

    def __init__(self, tool_name: str, args: dict) -> None:
        self.tool_name = tool_name
        self.args = args
        self.status = "pending"
        self.result_text = ""
        super().__init__(title=self._summary(), collapsed=True)

    def _summary(self) -> str:
        glyph = {"pending": "?", "done": "+", "denied": "x"}.get(self.status, "?")
        arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(self.args.items())[:2])
        return f"[{glyph}] {self.tool_name}({arg_preview})"

    def compose(self) -> ComposeResult:
        yield Static(self._body(), id="tool-body")

    def _body(self) -> str:
        lines = [f"args: {self.args}"]
        if self.result_text:
            lines.append("")
            lines.append(self.result_text)
        return "\n".join(lines)

    def _refresh(self) -> None:
        self.title = self._summary()
        try:
            self.query_one("#tool-body", Static).update(self._body())
        except Exception:
            pass

    def finish(self, result_text: str, status: str = "done") -> None:
        self.status = status
        self.result_text = result_text
        self._refresh()


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"› {text}")


class AssistantMessage(Static):
    """Streaming assistant text; append deltas as they arrive."""

    def __init__(self) -> None:
        self._text = ""
        super().__init__("")

    def append(self, delta: str) -> None:
        self._text += delta
        self.update(self._text)
