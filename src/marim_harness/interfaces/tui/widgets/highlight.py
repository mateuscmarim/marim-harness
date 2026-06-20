"""Shared syntax-highlighting helpers for the tool/diff renderers.

Used by both :mod:`.diff` (banded file diffs) and :mod:`.tools` (read_file /
write_file bodies), so they live here rather than in either consumer.
"""

import re

from rich.style import Style
from rich.syntax import Syntax
from rich.text import Span, Text

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


def _strip_bg(style):
    """Drop a style's background so only the diff band decides the line color.
    Rich's Syntax bakes a background in two ways — a whole-line ``"on default"``
    string span and a ``default`` bgcolor on each token; left on, context rows
    render with the terminal default (often black) instead of inheriting the
    widget background. Foreground and text attributes are preserved."""
    if isinstance(style, str):
        try:
            style = Style.parse(style)
        except Exception:
            return style
    if not isinstance(style, Style) or style.bgcolor is None:
        return style
    return Style(
        color=style.color, bold=style.bold, dim=style.dim, italic=style.italic,
        underline=style.underline, blink=style.blink, reverse=style.reverse,
        conceal=style.conceal, strike=style.strike,
    )


def _highlight_lines(text: str, lexer: "str | None") -> list[Text]:
    """Syntax-highlight ``text`` and split it into one ``Text`` per source line,
    aligned 1:1 with ``text.split("\\n")`` so a line number indexes its row. Plain
    ``Text`` per line when there's no lexer or highlighting fails or mis-aligns."""
    plain = text.split("\n")
    if not lexer:
        return [Text(line) for line in plain]
    try:
        highlighted = Syntax(text, lexer, background_color="default").highlight(text)
        lines = list(highlighted.split("\n"))
    except Exception:
        return [Text(line) for line in plain]
    # Strip the syntax-baked background so the diff band (or none) owns each line's
    # background — otherwise context rows show a stray default-bg box.
    for line in lines:
        line.style = _strip_bg(line.style)
        line.spans = [Span(s.start, s.end, _strip_bg(s.style)) for s in line.spans]
    # Rich's Text.split drops the trailing empty line that str.split keeps for a
    # newline-terminated file; pad it back so a line number indexes its row.
    while len(lines) < len(plain):
        lines.append(Text(""))
    return lines[: len(plain)] if len(lines) >= len(plain) else [Text(x) for x in plain]
