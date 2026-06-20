"""Tool-call log widgets: a single call (``ToolCallWidget``) and a folded run of
consecutive calls (``ToolGroupWidget``). ``ToolCallWidget`` special-cases the
file tools — an inline diff for ``edit_file``, highlighted content for
``write_file``/``read_file``."""

from pathlib import Path

from rich.console import RenderableType
from rich.text import Text
from textual.containers import Vertical
from textual.content import Content
from textual.widgets import Collapsible, Static

from .diff import _DIFF_CAP, _reverse_edits, render_edit_diff, render_file_diff
from .highlight import _LEXERS, _highlight_lines, strip_line_numbers


class ToolCallWidget(Collapsible):
    """A single tool call: the (clickable) title shows a summary line; expanding
    reveals the arguments and the result."""

    def __init__(self, tool_name: str, args: dict) -> None:
        self.tool_name = tool_name
        self.args = args
        self.status = "pending"
        self.result_text = ""
        # Show edit diffs uncapped (Ctrl+O / "reveal all" flips this on).
        self.reveal = False
        # Post-edit file text + reconstructed pre-edit text, loaded at finish() so
        # edit_file renders a real before/after diff (gutter line numbers, context,
        # bands). None until then ⇒ the simple old/new-string diff is the fallback.
        self._old_text: str | None = None
        self._new_text: str | None = None
        # markup=False: tool args/results are arbitrary text (commands, file
        # content, output) that may contain Rich markup syntax like `[/]`.
        self._body = Static(self._render_body(), id="tool-body", markup=False)
        # edit_file auto-expands so its diff shows inline; everything else stays
        # collapsed and click-to-expand.
        # title is a Content (not str) on purpose — see _summary; Textual renders
        # it at runtime, but its stub types title as str.
        super().__init__(
            self._body,
            title=self._summary(),  # pyright: ignore[reportArgumentType]
            collapsed=tool_name != "edit_file",
        )

    def _summary(self) -> Content:
        glyph = {"pending": "·", "done": "✓", "denied": "✕"}.get(self.status, "·")
        # edit_file: show just the path + a +N -M line stat (the diff is the body).
        if self.tool_name == "edit_file":
            _, added, removed = self._edit_diff(cap=None)
            path = self.args.get("path", "")
            return Content(f"{glyph} edit_file({path}) +{added} -{removed}")
        arg_preview = ", ".join(f"{k}={v!r}" for k, v in list(self.args.items())[:2])
        # Collapsible titles are parsed as Textual markup; the arg preview is
        # untrusted (file content, commands) and may contain bracket sequences
        # like `[edit(x="…` that escape() does NOT neutralise but the parser
        # still chokes on. A literal Content bypasses markup parsing entirely.
        return Content(f"{glyph} {self.tool_name}({arg_preview})")

    def _highlight(self, code: str, path: str) -> RenderableType:
        """Syntax-highlight ``code`` by the file's extension into a single Text with
        the syntax-baked background stripped, so it inherits the widget background
        instead of rendering a stray dark box. Plain ``code`` on a lexer miss."""
        lexer = _LEXERS.get(Path(path).suffix.lower())
        if not lexer:
            return code
        out = Text()
        for i, line in enumerate(_highlight_lines(code, lexer)):
            if i:
                out.append("\n")
            out.append_text(line)
        return out

    def _result_renderable(self) -> RenderableType:
        """The result body, syntax-highlighted when it is file source."""
        if self.tool_name == "read_file" and self.result_text:
            return self._highlight(
                strip_line_numbers(self.result_text), str(self.args.get("path", ""))
            )
        return self.result_text

    def _primary_renderable(self) -> "RenderableType | None":
        """The per-tool body that replaces the raw arg repr: a diff for edit_file,
        highlighted content for write_file. None for tools rendered generically."""
        if self.tool_name == "edit_file":
            cap = None if self.reveal else _DIFF_CAP
            diff, _, _ = self._edit_diff(cap=cap)
            return diff
        if self.tool_name == "write_file":
            return self._highlight(
                str(self.args.get("content", "")), str(self.args.get("path", ""))
            )
        return None

    def _edit_diff(self, *, cap):
        """The edit_file diff renderable + (added, removed) counts: a real
        before/after file diff once the file text is loaded, else the simple
        old/new-string diff."""
        if self._old_text is not None and self._new_text is not None:
            lexer = _LEXERS.get(Path(str(self.args.get("path", ""))).suffix.lower())
            return render_file_diff(
                self._old_text, self._new_text, cap=cap, lexer=lexer
            )
        return render_edit_diff(self.args.get("edits", []), cap=cap)

    def _load_diff(self) -> None:
        """Read the just-edited file and reconstruct its pre-edit text so the body
        can render a real diff. Best-effort: any failure (no app context, unreadable
        file, ambiguous reversal) leaves ``_old_text``/``_new_text`` None and the
        simple diff in place."""
        try:
            root = self.app.harness.deps.workspace_root  # type: ignore[attr-defined]
        except Exception:
            return
        from ....tools import fs

        try:
            path = fs._safe(root, str(self.args.get("path", "")))
            new_text = path.read_text(encoding="utf-8")
        except Exception:
            return
        old_text = _reverse_edits(new_text, self.args.get("edits", []))
        if old_text is not None:
            self._old_text, self._new_text = old_text, new_text

    def _render_body(self) -> RenderableType:
        from rich.console import Group

        primary = self._primary_renderable()
        if primary is not None:
            if not self.result_text:
                return primary
            # Text() keeps the result literal inside the Group (markup=False).
            return Group(primary, "", Text(self.result_text))

        arg_lines = "\n".join(f"{k}: {v!r}" for k, v in self.args.items())
        if not self.result_text:
            return arg_lines or "(no arguments)"
        result = self._result_renderable()
        if isinstance(result, str):
            return f"{arg_lines}\n\n{result}" if arg_lines else result
        # Text() keeps arg_lines literal: a bare str inside a Group would be
        # markup-parsed by Rich even though the host Static has markup=False.
        return Group(Text(arg_lines), "", result)

    def set_reveal(self, value: bool) -> None:
        """Reveal (uncap) the edit diff and expand, or restore the default
        capped/collapsed state. Driven by the app's Ctrl+O reveal-all toggle."""
        self.reveal = value
        self.collapsed = False if value else (self.tool_name != "edit_file")
        self._body.update(self._render_body())

    def finish(self, result_text: str, status: str = "done") -> None:
        self.status = status
        self.result_text = result_text
        if self.tool_name == "edit_file" and status == "done":
            self._load_diff()
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
