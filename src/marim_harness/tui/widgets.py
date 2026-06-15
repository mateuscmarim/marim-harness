import re
from pathlib import Path

from rich.console import RenderableType
from rich.syntax import Syntax
from textual.widgets import Collapsible, Markdown, Static

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


class AssistantMessage(Markdown):
    """Streaming assistant text rendered as Markdown; append deltas as they
    arrive and the view re-renders."""

    def __init__(self) -> None:
        self.text = ""
        super().__init__("")

    def append(self, delta: str) -> None:  # type: ignore[override]
        self.text += delta
        self.update(self.text)
