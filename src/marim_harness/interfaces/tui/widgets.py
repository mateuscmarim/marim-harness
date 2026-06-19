import re
from pathlib import Path

from rich.console import RenderableType
from rich.syntax import Syntax
from rich.text import Text
from textual import events
from textual.containers import Vertical
from textual.content import Content
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


def format_cost(cost: float) -> str:
    """Render a USD cost compactly — four decimals below a cent so small spends
    don't collapse to ``$0.00``, two decimals above: ``$0.0042``, ``$0.07``."""
    return f"${cost:.4f}" if cost < 0.01 else f"${cost:.2f}"


def format_token_split(usage) -> str:
    """The compact status-bar token split: ``1k↑ 55k⚡ 2k↓`` — ``↑`` uncached
    input, ``⚡`` cached (read + write), ``↓`` output. All three buckets always
    render (even at zero) so the bar keeps a stable width."""
    from ...usage import split_tokens

    s = split_tokens(usage)
    return (
        f"{human_tokens(s.uncached_input)}↑ "
        f"{human_tokens(s.cached_input)}⚡ "
        f"{human_tokens(s.output)}↓"
    )


class ToolCallWidget(Collapsible):
    """A single tool call: the (clickable) title shows a summary line; expanding
    reveals the arguments and the result."""

    def __init__(self, tool_name: str, args: dict) -> None:
        self.tool_name = tool_name
        self.args = args
        self.status = "pending"
        self.result_text = ""
        # markup=False: tool args/results are arbitrary text (commands, file
        # content, output) that may contain Rich markup syntax like `[/]`.
        self._body = Static(self._render_body(), id="tool-body", markup=False)
        # title is a Content (not str) on purpose — see _summary; Textual renders
        # it at runtime, but its stub types title as str.
        super().__init__(
            self._body, title=self._summary(), collapsed=True  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        glyph = {"pending": "·", "done": "✓", "denied": "✕"}.get(self.status, "·")
        arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(self.args.items())[:2])
        # Collapsible titles are parsed as Textual markup; the arg preview is
        # untrusted (file content, commands) and may contain bracket sequences
        # like `[edit(x="…` that escape() does NOT neutralise but the parser
        # still chokes on. A literal Content bypasses markup parsing entirely.
        return Content(f"{glyph} {self.tool_name}({arg_preview})")

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

        # Text() keeps arg_lines literal: a bare str inside a Group would be
        # markup-parsed by Rich even though the host Static has markup=False.
        return Group(Text(arg_lines), "", result)

    def finish(self, result_text: str, status: str = "done") -> None:
        self.status = status
        self.result_text = result_text
        self.title = self._summary()
        self._body.update(self._render_body())


class ToolGroupWidget(Collapsible):
    """A run of consecutive tool calls folded into one collapsed row. Its children
    are the individual ToolCallWidgets; the title summarizes the batch (total count
    plus a per-tool breakdown), so a burst of reads is one line, not N.

    A group is only created once a run has two-or-more calls — a lone call stays a
    bare ToolCallWidget, since wrapping one tool adds a redundant header and an
    extra click for no grouping benefit. Starts collapsed for that reason."""

    def __init__(self) -> None:
        # Insertion-ordered count per tool name, for the title breakdown.
        self._counts: dict[str, int] = {}
        self.body = Vertical(classes="tool-group-body")
        # title is a Content (not str) on purpose — see _summary.
        super().__init__(
            self.body, title=self._summary(), collapsed=True  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        total = sum(self._counts.values())
        label = "1 tool" if total == 1 else f"{total} tools"
        # "read_file ×3 · grep" — only show the multiplier when it repeats.
        parts = [f"{name} ×{n}" if n > 1 else name for name, n in self._counts.items()]
        breakdown = " · ".join(parts)
        text = f"≡ {label} · {breakdown}" if breakdown else f"≡ {label}"
        # Tool names are our own literals, but bypass markup parsing anyway for
        # consistency with the other Collapsible titles in this module.
        return Content(text)

    async def add_tool(self, widget: ToolCallWidget) -> None:
        """Mount a tool call into the group and refresh the summary line. The
        widget may already be mounted elsewhere (a lone call promoted into a new
        group); the caller detaches it first."""
        self._counts[widget.tool_name] = self._counts.get(widget.tool_name, 0) + 1
        self.title = self._summary()
        await self.body.mount(widget)


# These three log widgets carry arbitrary text — user input, exception strings,
# MCP errors — that may contain Rich markup syntax (e.g. a stray ``[/]``). Their
# glyph and colour come from CSS classes, not inline markup, so render with
# ``markup=False`` to show the text literally; otherwise a MarkupError raised
# during layout crashes the whole app.
class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"› {text}", classes="user-msg", markup=False)


class ErrorMessage(Static):
    """A turn that failed: shown in the log so the session survives the error."""

    def __init__(self, text: str) -> None:
        super().__init__(f"✕ {text}", classes="error-msg", markup=False)


class NoticeMessage(Static):
    """A low-key system note in the log (e.g. history was compacted)."""

    def __init__(self, text: str) -> None:
        super().__init__(f"· {text}", classes="notice-msg", markup=False)


class TurnMeta(Static):
    """A dim per-turn footer stamped under a reply — e.g. how long the turn took."""

    def __init__(self, text: str) -> None:
        super().__init__(f"· {text}", classes="turn-meta", markup=False)


class TaskPanel(Static):
    """The agent's live checklist, pinned above the status bar. Hidden whenever
    the list is empty so it takes no space when unused."""

    def __init__(self) -> None:
        super().__init__(id="task-panel")
        self.display = False

    def show_tasks(self, items: list) -> None:
        """Render the current checklist, or hide the panel when there are none."""
        from ...tasks import render_tasks

        if not items:
            self.display = False
            self.update("")
            return
        self.display = True
        # The header is intentional markup; the task body is untrusted and may
        # contain bracket sequences that escape() can't neutralise, so render it
        # as a literal Content appended to the parsed header.
        self.update(Content.from_markup("[b $accent]Tasks[/]\n") + Content(render_tasks(items)))


class JobPanel(Static):
    """The session's live background jobs, pinned above the status bar (a sibling
    of the task panel). Hidden whenever there are no jobs."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ...jobs import render_jobs

        if not jobs:
            self.display = False
            self.update("")
            return
        self.display = True
        # The header is intentional markup; the job labels are untrusted and may
        # contain bracket sequences that escape() can't neutralise, so render them
        # as a literal Content appended to the parsed header.
        self.update(Content.from_markup("[b $accent]Jobs[/]\n") + Content(render_jobs(jobs)))


class SubAgentWidget(Collapsible):
    """A spawned sub-agent: the title summarizes the delegation; the (expanded)
    body is a live stream of the sub-agent's own text and tool calls, mounted as
    child widgets as its events arrive."""

    DEFAULT_CSS = """
    SubAgentWidget .subagent-usage {
        color: $text-muted;
    }
    """

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
        # Live token usage. The total + cost ride in the (collapsed) title so a
        # fan-out exposes each agent's consumption at a glance; the full cache
        # split is reserved for the expanded body, where there's room for it.
        self.tokens = 0
        self.cost_text: str | None = None
        self.split_text = ""
        # A muted header line inside the expanded body carrying the detailed
        # split + cost (mirrors the session status bar). Hidden until populated
        # so an as-yet-unmetered agent doesn't show a blank line.
        self._usage_line = Static("", classes="subagent-usage")
        self._usage_line.display = False
        self.body = Vertical(self._usage_line, classes="subagent-body")
        # title is a Content (not str) on purpose — see _summary.
        super().__init__(
            self.body, title=self._summary(), collapsed=collapsed  # pyright: ignore[reportArgumentType]
        )

    def _summary(self) -> Content:
        glyph = {"pending": "▸", "done": "✓", "denied": "✕"}.get(self.status, "▸")
        task = self.agent_task if len(self.agent_task) <= 40 else self.agent_task[:39] + "…"
        parts = [f"{glyph} spawn_agent({self.agent_type}: {task!r})"]
        # Only a running agent carries an activity tail; a finished one is clean.
        if self.status == "pending" and self.activity:
            parts.append(self.activity)
        # Token count and cost persist across finish — the final spend stays
        # visible. The three-way split is intentionally NOT here: it would bloat
        # the title and hurt fan-out legibility, so it lives in the body instead.
        if self.tokens:
            parts.append(f"{human_tokens(self.tokens)} tok")
        if self.cost_text:
            parts.append(self.cost_text)
        # Collapsible titles are parsed as Textual markup; the task text is
        # untrusted and may contain bracket sequences escape() can't neutralise,
        # so a literal Content bypasses markup parsing entirely.
        return Content(" · ".join(parts))

    def set_tokens(self, n: int) -> None:
        """Update the sub-agent's running token total and refresh the title."""
        self.tokens = n
        self.title = self._summary()

    def set_usage(self, total: int, cost_text: str | None, split_text: str) -> None:
        """Fold a full usage reading in: the title shows the running ``total`` (and
        ``cost_text`` when priced), while the expanded body's muted header shows the
        detailed ``split_text`` + cost — the status-bar view, where there's room."""
        self.cost_text = cost_text
        self.split_text = split_text
        self.set_tokens(total)  # updates the token total + repaints the title
        self._refresh_usage_line()

    def _refresh_usage_line(self) -> None:
        detail = self.split_text
        if self.cost_text:
            detail = f"{detail} · {self.cost_text}" if detail else self.cost_text
        self._usage_line.update(detail)
        self._usage_line.display = bool(detail)

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


_IMAGE_MARKER = re.compile(r"\[Image #(\d+)\]")


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
        """Posted when the user presses Enter; carries the box's full text and
        any attached images as (bytes, media_type) tuples."""

        def __init__(self, value: str,
                     attachments: list[tuple[bytes, str]] | None = None) -> None:
            self.value = value
            self.attachments = attachments or []
            super().__init__()

    def __init__(self, history=None) -> None:
        from ...history import PromptHistory

        # NB: TextArea.history is its own undo stack — keep prompt history apart.
        self.prompt_history = history if history is not None else PromptHistory()
        # Navigation cursor into history.entries; None means "editing the live
        # draft". ``_draft`` stashes that draft while scrolling back.
        self._hist_idx: int | None = None
        self._draft = ""
        super().__init__(soft_wrap=True, show_line_numbers=False)
        self.attachments: list[tuple[Path, str]] = []

    async def _on_key(self, event: events.Key) -> None:
        if event.key == "enter":
            event.prevent_default()
            event.stop()
            atts = [(p.read_bytes(), m) for p, m in self.attachments]
            self.post_message(self.Submitted(self.text, atts))
            self.attachments = []
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
        if event.key == "ctrl+v":
            if self._on_paste_image():
                event.prevent_default()
                event.stop()
                return
        if event.key in ("backspace", "delete") and self._delete_markers(event.key):
            event.prevent_default()
            event.stop()
            return
        await super()._on_key(event)

    def _on_paste_image(self) -> bool:
        from ... import images

        got = images.read_clipboard_image()
        if got is None:
            return False
        data, media_type = got
        return self._cache_and_insert(data, media_type)

    def _cache_and_insert(self, data: bytes, media_type: str) -> bool:
        from ... import images

        cached = images.store_image(self._session_id(), data, media_type)
        self.attachments.append((cached.path, media_type))
        self.insert(f"[Image #{len(self.attachments)}]")
        return True

    def _session_id(self) -> str:
        # Resolve lazily from the running app's harness; fall back to a constant
        # bucket if unavailable (e.g. isolated widget tests). Persistence (the
        # externalize task) re-stores under the real session id regardless, so a
        # fallback bucket here only affects the transient paste-time cache path.
        try:
            return self.app.harness.session.store.session_id  # type: ignore[attr-defined]
        except Exception:
            return "default"

    def on_paste(self, event: events.Paste) -> None:
        from ... import images

        path = images.detect_image_path(event.text)
        if path is None:
            return  # let TextArea insert the pasted text normally
        media_type = images.media_type_for_path(path)
        if media_type is None:
            return
        event.prevent_default()
        event.stop()
        self._cache_and_insert(path.read_bytes(), media_type)

    def _offset(self, loc: tuple[int, int]) -> int:
        """Absolute character offset of a (row, col) cursor location."""
        row, col = loc
        lines = self.text.split("\n")
        return sum(len(line) + 1 for line in lines[:row]) + col

    def _location(self, offset: int) -> tuple[int, int]:
        """The (row, col) location of an absolute character offset in self.text."""
        head = self.text[:offset]
        return (head.count("\n"), offset - (head.rfind("\n") + 1))

    def _delete_markers(self, key: str) -> bool:
        """Keep ``[Image #N]`` markers atomic: if a backspace/delete touches any
        part of a marker (including its brackets), remove the whole marker and
        drop the matching attachment instead of breaking the text. Surviving
        markers renumber so they stay ``#1..#M`` aligned with ``attachments``.
        Returns True when it consumed the edit, False to fall through to the
        normal TextArea editing."""
        text = self.text
        spans = [(m.start(), m.end(), int(m.group(1)))
                 for m in _IMAGE_MARKER.finditer(text)]
        if not spans:
            return False
        lo = self._offset(self.selection.start)
        hi = self._offset(self.selection.end)
        if lo > hi:
            lo, hi = hi, lo
        if lo == hi:  # no selection — a single-character edit
            if key == "backspace":
                if lo == 0:
                    return False
                lo -= 1
            else:  # delete
                if hi >= len(text):
                    return False
                hi += 1
        hit = [s for s in spans if s[0] < hi and s[1] > lo]
        if not hit:
            return False
        lo = min(lo, min(s[0] for s in hit))
        hi = max(hi, max(s[1] for s in hit))
        removed = {s[2] for s in hit}
        for n in sorted(removed, reverse=True):
            if 1 <= n <= len(self.attachments):
                del self.attachments[n - 1]

        def _renumber(m: "re.Match[str]") -> str:
            n = int(m.group(1))
            return f"[Image #{n - sum(r < n for r in removed)}]"

        new_prefix = _IMAGE_MARKER.sub(_renumber, text[:lo])
        new_text = new_prefix + _IMAGE_MARKER.sub(_renumber, text[hi:])
        self.text = new_text
        self.move_cursor(self._location(len(new_prefix)))
        return True

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
        # +2 for the box border's top and bottom rows (see styles.tcss), so the
        # visible text area, not the outer box, tracks the [min, max] window.
        self.styles.height = self._target_height() + 2

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
