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


def human_tokens(n: int) -> str:
    """Compact token count: 950 -> '950', 1500 -> '1.5k', 100000 -> '100k'."""
    if n >= 1000:
        return f"{n / 1000:.1f}k".replace(".0k", "k")
    return str(n)


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


class JobPanel(Static):
    """The session's live background jobs, pinned above the status bar (a sibling
    of the task panel). Hidden whenever there are no jobs."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ..jobs import render_jobs

        if not jobs:
            self.display = False
            self.update("")
            return
        self.display = True
        self.update("Jobs\n" + render_jobs(jobs))


class SubAgentWidget(Collapsible):
    """A spawned sub-agent: the title summarizes the delegation; the (expanded)
    body is a live stream of the sub-agent's own text and tool calls, mounted as
    child widgets as its events arrive."""

    def __init__(
        self, agent_type: str, agent_task: str, collapsed: bool = False
    ) -> None:
        self.agent_type = agent_type
        self.agent_task = agent_task
        self.status = "pending"
        self.report = ""
        # Live activity shown in the (collapsed) title so a fan-out of agents is
        # legible at a glance without expanding each stream.
        self.activity = ""
        self.tool_count = 0
        # Live token usage, shown in the (collapsed) title so a fan-out of agents
        # exposes each one's consumption at a glance.
        self.tokens = 0
        self.body = Vertical(classes="subagent-body")
        super().__init__(self.body, title=self._summary(), collapsed=collapsed)

    def _summary(self) -> str:
        glyph = {"pending": "▸", "done": "+", "denied": "x"}.get(self.status, "▸")
        task = self.agent_task if len(self.agent_task) <= 40 else self.agent_task[:39] + "…"
        parts = [f"[{glyph}] spawn_agent({self.agent_type}: {task!r})"]
        # Only a running agent carries an activity tail; a finished one is clean.
        if self.status == "pending" and self.activity:
            parts.append(self.activity)
        # The token count persists across finish — the final cost stays visible.
        if self.tokens:
            parts.append(f"{human_tokens(self.tokens)} tok")
        return " · ".join(parts)

    def set_tokens(self, n: int) -> None:
        """Update the sub-agent's running token total and refresh the title."""
        self.tokens = n
        self.title = self._summary()

    def note_tool(self, tool_name: str) -> None:
        """Record that the sub-agent just called ``tool_name`` and refresh the
        title — a cheap status update that needs no body mount."""
        self.tool_count += 1
        self.activity = f"{tool_name} ({self.tool_count})"
        self.title = self._summary()

    def note_text(self) -> None:
        """Record that the sub-agent is generating text and refresh the title."""
        self.activity = "responding"
        self.title = self._summary()

    async def add(self, widget) -> None:
        """Mount a child widget (the sub-agent's text or a nested tool call) into
        the live body."""
        await self.body.mount(widget)

    def finish(self, report: str, status: str = "done") -> None:
        self.status = status
        self.report = report
        self.activity = ""
        self.title = self._summary()


class PromptInput(TextArea):
    """The multi-line message box. Enter submits; Shift+Enter and Ctrl+J insert a
    newline. The box auto-grows with its content up to ``_MAX_LINES``, then
    scrolls internally.

    Up/Down recall previously submitted prompts shell-style — but only at the
    text boundaries (Up on the first line, Down on the last), so inside a
    multi-line draft the arrows still move the cursor normally."""

    _MIN_LINES = 3
    _MAX_LINES = 10

    class Submitted(Message):
        """Posted when the user presses Enter; carries the box's full text."""

        def __init__(self, value: str) -> None:
            self.value = value
            super().__init__()

    def __init__(self, history=None) -> None:
        from ..history import PromptHistory

        # NB: TextArea.history is its own undo stack — keep prompt history apart.
        self.prompt_history = history if history is not None else PromptHistory()
        # Navigation cursor into history.entries; None means "editing the live
        # draft". ``_draft`` stashes that draft while scrolling back.
        self._hist_idx: int | None = None
        self._draft = ""
        super().__init__(soft_wrap=True, show_line_numbers=False)

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            self.post_message(self.Submitted(self.text))
            self._reset_nav()
            return
        if event.key in ("shift+enter", "ctrl+j"):
            event.prevent_default()
            event.stop()
            self.insert("\n")
            return
        if event.key == "up" and self._at_first_line() and self._recall_prev():
            event.prevent_default()
            event.stop()
            return
        if event.key == "down" and self._at_last_line() and self._recall_next():
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    def _at_first_line(self) -> bool:
        return self.cursor_location[0] == 0

    def _at_last_line(self) -> bool:
        return self.cursor_location[0] == self.document.line_count - 1

    def _reset_nav(self) -> None:
        self._hist_idx = None
        self._draft = ""

    def _show(self, text: str) -> None:
        """Replace the box with ``text`` and drop the cursor at the end."""
        self.text = text
        self.move_cursor(self.document.end)

    def _recall_prev(self) -> bool:
        """Move one step back into history. Returns whether it consumed the key."""
        entries = self.prompt_history.entries
        if not entries:
            return False
        if self._hist_idx is None:
            self._draft = self.text  # remember what we were typing
            self._hist_idx = len(entries) - 1
        elif self._hist_idx > 0:
            self._hist_idx -= 1
        # else: already at the oldest — stay put, but still consume the key.
        self._show(entries[self._hist_idx])
        return True

    def _recall_next(self) -> bool:
        """Move one step forward; past the newest entry restores the draft."""
        if self._hist_idx is None:
            return False  # not navigating — let Down move the cursor
        entries = self.prompt_history.entries
        if self._hist_idx < len(entries) - 1:
            self._hist_idx += 1
            self._show(entries[self._hist_idx])
        else:
            self._hist_idx = None
            self._show(self._draft)
        return True

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
    """Streaming assistant text rendered as Markdown. ``append`` only buffers the
    delta — re-parsing the whole markdown document on every token is O(n²) and
    makes streaming janky — so the (expensive) render is deferred to ``flush``,
    which the app drives on a shared interval to coalesce many deltas into one
    parse."""

    def __init__(self) -> None:
        self.text = ""
        self._pending = False
        super().__init__("")

    def append(self, delta: str) -> None:  # type: ignore[override]
        self.text += delta
        self._pending = True

    def flush(self) -> bool:
        """Render the buffered text if there is any. Returns whether it rendered,
        so the caller can skip scroll/update work when nothing changed."""
        if not self._pending:
            return False
        self.update(self.text)
        self._pending = False
        return True
