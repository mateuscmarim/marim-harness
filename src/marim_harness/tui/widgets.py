import re
from pathlib import Path

from rich.console import RenderableType
from rich.syntax import Syntax
from textual import events
from textual.containers import Vertical
from textual.message import Message
from textual.widgets import Collapsible, Markdown, Static, TextArea

_LINE_PREFIX = re.compile(r"^\s*\d+\t", re.MULTILINE)

# read_file (and the like) emit "N\t<line>" rows; map the file extension to a
# lexer so the expanded body is syntax-highlighted instead of raw text.
_LEXERS = {
    ".py": "python",
    ".js": "javascript",
    ".ts": "typescript",
    ".tsx": "tsx",
    ".jsx": "jsx",
    ".json": "json",
    ".toml": "toml",
    ".yaml": "yaml",
    ".yml": "yaml",
    ".md": "markdown",
    ".sh": "bash",
    ".css": "css",
    ".html": "html",
    ".rs": "rust",
    ".go": "go",
    ".sql": "sql",
}


def strip_line_numbers(text: str) -> str:
    """Drop the leading "N\\t" line-number prefixes the read tools add."""
    return _LINE_PREFIX.sub("", text)


class ToolCallWidget(Collapsible):
    """A single tool call: the (clickable) title shows a summary line; expanding
    reveals the arguments and the result."""

    def __init__(self, tool_name: str, args: dict) -> None:
        self.tool_name = tool_name
        self.args = args
        self.status = "pending"
        self.result_text = ""
        self._body = Static(self._render_body(), id="tool-body")
        super().__init__(self._body, title=self._summary(), collapsed=True)

    def _summary(self) -> str:
        glyph = {"pending": "?", "done": "+", "denied": "x"}.get(self.status, "?")
        arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(self.args.items())[:2])
        return f"[{glyph}] {self.tool_name}({arg_preview})"

    def _result_renderable(self) -> RenderableType:
        """The result body, syntax-highlighted when it is file source."""
        if self.tool_name == "read_file" and self.result_text:
            path = str(self.args.get("path", ""))
            lexer = _LEXERS.get(Path(path).suffix.lower())
            if lexer:
                code = strip_line_numbers(self.result_text)
                try:
                    return Syntax(code, lexer, background_color="default", word_wrap=True)
                except Exception:
                    return code
        return self.result_text

    def _render_body(self) -> RenderableType:
        arg_lines = "\n".join(f"{k}: {v!r}" for k, v in self.args.items())
        if not self.result_text:
            return arg_lines or "(no arguments)"
        result = self._result_renderable()
        if isinstance(result, str):
            return f"{arg_lines}\n\n{result}" if arg_lines else result
        from rich.console import Group

        return Group(arg_lines, "", result)

    def finish(self, result_text: str, status: str = "done") -> None:
        self.status = status
        self.result_text = result_text
        self.title = self._summary()
        self._body.update(self._render_body())


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"› {text}", classes="user-msg")


class ErrorMessage(Static):
    """A turn that failed: shown in the log so the session survives the error."""

    def __init__(self, text: str) -> None:
        super().__init__(f"⚠ {text}", classes="error-msg")


class NoticeMessage(Static):
    """A low-key system note in the log (e.g. history was compacted)."""

    def __init__(self, text: str) -> None:
        super().__init__(f"• {text}", classes="notice-msg")


class TaskPanel(Static):
    """The agent's live checklist, pinned above the status bar. Hidden whenever
    the list is empty so it takes no space when unused."""

    def __init__(self) -> None:
        super().__init__(id="task-panel")
        self.display = False

    def show_tasks(self, items: list) -> None:
        """Render the current checklist, or hide the panel when there are none."""
        from ..tasks import render_tasks

        if not items:
            self.display = False
            self.update("")
            return
        self.display = True
        self.update("Tasks\n" + render_tasks(items))


class SubAgentWidget(Collapsible):
    """A spawned sub-agent: the title summarizes the delegation; the (expanded)
    body is a live stream of the sub-agent's own text and tool calls, mounted as
    child widgets as its events arrive."""

    def __init__(self, agent_type: str, agent_task: str) -> None:
        self.agent_type = agent_type
        self.agent_task = agent_task
        self.status = "pending"
        self.report = ""
        self.body = Vertical(classes="subagent-body")
        super().__init__(self.body, title=self._summary(), collapsed=False)

    def _summary(self) -> str:
        glyph = {"pending": "▸", "done": "+", "denied": "x"}.get(self.status, "▸")
        task = self.agent_task if len(self.agent_task) <= 40 else self.agent_task[:39] + "…"
        return f"[{glyph}] spawn_agent({self.agent_type}: {task!r})"

    async def add(self, widget) -> None:
        """Mount a child widget (the sub-agent's text or a nested tool call) into
        the live body."""
        await self.body.mount(widget)

    def finish(self, report: str, status: str = "done") -> None:
        self.status = status
        self.report = report
        self.title = self._summary()


class PromptInput(TextArea):
    """The multi-line message box. Enter submits; Shift+Enter and Ctrl+J insert a
    newline. The box auto-grows with its content up to ``_MAX_LINES``, then
    scrolls internally."""

    _MIN_LINES = 3
    _MAX_LINES = 10

    class Submitted(Message):
        """Posted when the user presses Enter; carries the box's full text."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self) -> None:
        super().__init__(soft_wrap=True, show_line_numbers=False)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        await super()._on_key(event)

    def _target_height(self) -> int:
        """Rows the box should occupy: one per logical line, clamped to the
        [min, max] window."""
        lines = self.document.line_count
        return max(self._MIN_LINES, min(lines, self._MAX_LINES))

    def _resize(self) -> None:
        self.styles.height = self._target_height()

    def on_text_area_changed(self, event: "TextArea.Changed") -> None:
        self._resize()


class AssistantMessage(Markdown):
    """Streaming assistant text rendered as Markdown; append deltas as they
    arrive and the view re-renders."""

    def __init__(self) -> None:
        self.text = ""
        super().__init__("")

    def append(self, delta: str) -> None:  # type: ignore[override]
        self.text += delta
        self.update(self.text)
