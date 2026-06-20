"""edit_file / file diff rendering.

Two renderers: ``render_edit_diff`` (a simple red/green ``- old`` / ``+ new`` diff
straight from the edit strings, no file reads) and ``render_file_diff`` (a
Claude-style banded diff with real gutter line numbers and context, built from the
before/after file text). ``_reverse_edits`` reconstructs the pre-edit text so the
widget can use the richer renderer.
"""

import difflib
from dataclasses import dataclass

from rich.segment import Segment
from rich.style import Style
from rich.text import Text

from .highlight import _highlight_lines

# Lines of an edit_file diff shown inline before truncating (Ctrl+O reveals all).
_DIFF_CAP = 20


def render_edit_diff(edits, *, cap):
    """Render an ``edit_file`` call's edits as a red ``- old`` / green ``+ new``
    diff (one ``Text``), returning ``(text, added, removed)`` line counts. Each
    edit's old lines are removals, its new lines additions; edits are blank-line
    separated. When ``cap`` is an int and the diff exceeds it, the first ``cap``
    rows are kept and a dim ``… +M more lines (ctrl+o)`` footer is appended;
    ``cap=None`` shows everything. Pure — no file reads."""
    rows: list[tuple[str, str]] = []
    added = removed = 0
    for edit in edits or []:
        if not isinstance(edit, dict):
            continue
        old = str(edit.get("old_string", ""))
        new = str(edit.get("new_string", ""))
        if rows:
            rows.append(("", ""))  # blank line between edits
        for line in old.split("\n") if old else []:
            rows.append(("red", f"- {line}"))
            removed += 1
        for line in new.split("\n") if new else []:
            rows.append(("green", f"+ {line}"))
            added += 1

    if cap is not None and len(rows) > cap:
        shown, hidden = rows[:cap], len(rows) - cap
    else:
        shown, hidden = rows, 0
    text = Text()
    for i, (style, line) in enumerate(shown):
        if i:
            text.append("\n")
        text.append(line, style=style or None)
    if hidden:
        text.append("\n")
        text.append(f"… +{hidden} more lines (ctrl+o)", style="dim")
    return text, added, removed


# Claude-style banded diff colors, tuned for the neutral dark base (themes.py).
# Diffs stay red/green regardless of the accent theme — the colors are semantic,
# not chrome — so they live here as Rich styles rather than theme variables.
_ADD_BG = "#16321f"
_REM_BG = "#3a1d1d"
_GUTTER = "#6b7079"
_ADD_MARK = "#5fae7e"
_REM_MARK = "#d9544f"


@dataclass
class _DiffRow:
    """One rendered diff line. ``kind`` is ``context``/``add``/``remove``/``gap``;
    ``old_no``/``new_no`` are 1-based file line numbers (the gutter shows the old
    number for removals, the new number otherwise). ``gap`` rows mark elided
    unchanged regions between hunks."""

    kind: str
    old_no: "int | None"
    new_no: "int | None"
    text: str


def compute_diff_rows(old_text: str, new_text: str, context: int = 3):
    """Line-diff ``old_text`` vs ``new_text`` into ``_DiffRow``s with real file
    line numbers, grouping changes into hunks with ``context`` lines around each
    and a ``gap`` row between hunks. Returns ``(rows, added, removed)`` where the
    counts are total added/removed lines (pre-cap). Pure."""
    old_lines = old_text.split("\n")
    new_lines = new_text.split("\n")
    sm = difflib.SequenceMatcher(a=old_lines, b=new_lines, autojunk=False)
    rows: list[_DiffRow] = []
    added = removed = 0
    for gi, group in enumerate(sm.get_grouped_opcodes(context)):
        if gi:
            rows.append(_DiffRow("gap", None, None, "⋮"))
        for tag, i1, i2, j1, j2 in group:
            if tag == "equal":
                for i, j in zip(range(i1, i2), range(j1, j2)):
                    rows.append(_DiffRow("context", i + 1, j + 1, old_lines[i]))
            else:
                for i in range(i1, i2):
                    rows.append(_DiffRow("remove", i + 1, None, old_lines[i]))
                    removed += 1
                for j in range(j1, j2):
                    rows.append(_DiffRow("add", None, j + 1, new_lines[j]))
                    added += 1
    return rows, added, removed


class EditDiff:
    """A Claude-style banded diff: a dim line-number gutter, a ``+``/``-`` marker,
    syntax-highlighted content, and a full-width red/green background band on
    changed lines. A custom renderable so each line can be padded to the console
    width (a plain ``Text`` background only colors its glyphs, not the full row)."""

    def __init__(
        self,
        rows: list[_DiffRow],
        hidden: int,
        old_hl: list[Text],
        new_hl: list[Text],
    ) -> None:
        self.rows = rows
        self.hidden = hidden
        self.old_hl = old_hl
        self.new_hl = new_hl
        nums = [n for r in rows for n in (r.old_no, r.new_no) if n is not None]
        self._gw = max((len(str(n)) for n in nums), default=1)

    def __rich_console__(self, console, options):
        width = options.max_width
        for row in self.rows:
            yield from self._render_row(console, row, width)
            yield Segment("\n")
        if self.hidden:
            yield Segment(
                f"… +{self.hidden} more lines (ctrl+o)", Style(dim=True)
            )
            yield Segment("\n")

    def _render_row(self, console, row: _DiffRow, width: int):
        if row.kind == "gap":
            yield Segment((" " * self._gw) + " ⋮", Style(color=_GUTTER, dim=True))
            return
        bg = {"add": _ADD_BG, "remove": _REM_BG}.get(row.kind)
        band = Style(bgcolor=bg) if bg else Style()
        num = row.old_no if row.kind == "remove" else row.new_no
        gutter = (str(num) if num is not None else "").rjust(self._gw)
        mark = {"add": "+", "remove": "-"}.get(row.kind, " ")
        mark_color = {"add": _ADD_MARK, "remove": _REM_MARK}.get(row.kind)
        yield Segment(gutter + " ", Style(color=_GUTTER) + band)
        yield Segment(mark + " ", Style(color=mark_color, bold=bool(bg)) + band)
        used = self._gw + 3  # gutter + space + marker + space

        src = self.old_hl if row.kind == "remove" else self.new_hl
        idx = (row.old_no if row.kind == "remove" else row.new_no)
        line = src[idx - 1].copy() if idx and idx - 1 < len(src) else Text(row.text)
        avail = max(0, width - used)
        line.truncate(avail, overflow="ellipsis")
        for seg in line.render(console, end=""):
            yield Segment(seg.text, (seg.style or Style()) + band)
        pad = width - used - line.cell_len
        if pad > 0 and bg:
            yield Segment(" " * pad, band)


def render_file_diff(
    old_text: str,
    new_text: str,
    *,
    cap,
    lexer: "str | None" = None,
    context: int = 3,
):
    """Render a real before/after file diff Claude-style — an ``EditDiff`` with
    gutter line numbers, context, and red/green bands — returning ``(renderable,
    added, removed)``. When ``cap`` is an int and the rows exceed it, the first
    ``cap`` are kept and a ``… +M more lines (ctrl+o)`` footer is shown."""
    rows, added, removed = compute_diff_rows(old_text, new_text, context)
    if cap is not None and len(rows) > cap:
        shown, hidden = rows[:cap], len(rows) - cap
    else:
        shown, hidden = rows, 0
    diff = EditDiff(shown, hidden, _highlight_lines(old_text, lexer),
                    _highlight_lines(new_text, lexer))
    return diff, added, removed


def _reverse_edits(new_text: str, edits) -> "str | None":
    """Reconstruct the pre-edit file text from the post-edit ``new_text`` by
    reverse-applying ``edits`` (swapping each ``new_string`` back to its
    ``old_string``, in reverse order). Returns the reconstruction only if
    re-applying the edits forward reproduces ``new_text`` exactly — otherwise
    ``None``, so an ambiguous reversal falls back to the simple diff rather than
    showing a wrong one. ``replace_all`` edits are a known best-effort limit."""
    if not edits or not all(isinstance(e, dict) for e in edits):
        return None
    old = new_text
    for e in reversed(edits):
        o, n = str(e.get("old_string", "")), str(e.get("new_string", ""))
        old = old.replace(n, o) if e.get("replace_all") else old.replace(n, o, 1)
    check = old
    for e in edits:
        o, n = str(e.get("old_string", "")), str(e.get("new_string", ""))
        if e.get("replace_all"):
            check = check.replace(o, n)
        else:
            if check.count(o) != 1:
                return None
            check = check.replace(o, n, 1)
    return old if check == new_text else None
