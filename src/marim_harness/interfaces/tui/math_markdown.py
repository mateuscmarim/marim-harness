"""LaTeX math rendering for the TUI's Markdown surfaces.

Models routinely emit LaTeX math in replies. A terminal cannot typeset it, but
it can show a readable Unicode approximation (``α² + √(β₁)``). This module
builds the ``parser_factory`` that ``AssistantMessage`` hands to Textual's
``Markdown`` widget: the stock "gfm-like" parser plus math recognition for the
four delimiter forms models actually emit (``$..$``, ``$$..$$``, ``\\(..\\)``,
``\\[..\\]``), with every math token rewritten *inside the parser* into
ordinary text/paragraph tokens whose content is the flatlatex transliteration.

Rewriting at the parser level keeps streaming self-correcting for free:
``Markdown.append`` re-parses the entire still-open trailing block from source
on every flush, so an unclosed ``\\(x^2`` renders literally and converts on
the flush after ``\\)`` arrives — no hold-back logic, and completed
(already-flushed) blocks are never rewritten.

Failure always degrades to literal LaTeX: an unparsable span renders verbatim
with its delimiters, and a missing flatlatex or ``MARIM_TUI_MATH=0`` makes
:func:`math_parser_factory` return ``None`` so Textual uses its stock parser.
Only the live TUI view transliterates — the persisted transcript and headless
output keep the lossless raw LaTeX.
"""

from __future__ import annotations

import os
import re
from collections.abc import Callable
from typing import TYPE_CHECKING

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.utils import is_code_block

if TYPE_CHECKING:
    from markdown_it.rules_block import StateBlock
    from markdown_it.rules_core import StateCore
    from markdown_it.rules_inline import StateInline

try:
    import flatlatex
except ImportError:  # pragma: no cover - exercised via monkeypatch in tests
    # The [tui] extra declares flatlatex, but an embedder with a trimmed
    # dependency set must get literal LaTeX, never an ImportError at render.
    flatlatex = None  # type: ignore[assignment]

# Mirrors config/model.py's _bool_env semantics (private there; four lines is
# cheaper than coupling a render-only module to the config package).
_TRUTHY = {"1", "true", "on", "yes"}

# Cached across calls: ~0.3 ms to build, and every math span is re-converted on
# each re-parse of the still-open block while a reply streams.
_converter = None

# flatlatex parenthesizes a big operator carrying both bounds — ``\int_0^1``
# becomes ``(∫₀)¹`` — which reads worse than the plain ``∫₀¹``. Strip those
# parens when (and only when) the group is a big operator plus subscripts and a
# superscript follows: ordinary grouping like ``(a₁)²`` has no big operator and
# never matches.
_SUBSCRIPTS = "₀₁₂₃₄₅₆₇₈₉₊₋₌₍₎ₐₑₒₓₔₕₖₗₘₙₚₛₜᵢᵣᵤᵥᵦᵧᵨᵩᵪ"
_SUPERSCRIPTS = "⁰¹²³⁴⁵⁶⁷⁸⁹⁺⁻⁼⁽⁾ⁿⁱ"
_BIG_OPS = "∫∬∭∮∯∰∑∏⋀⋁⋂⋃"
_BOUNDED_OP = re.compile(
    f"\\(([{_BIG_OPS}][{_SUBSCRIPTS}]+)\\)(?=\\^|[{_SUPERSCRIPTS}])"
)


def latex_to_unicode(src: str) -> str | None:
    """Transliterate the LaTeX math ``src`` (delimiters already stripped) to a
    Unicode approximation, or ``None`` when conversion is impossible — the
    caller decides what the literal fallback looks like. Never raises."""
    global _converter
    if flatlatex is None:
        return None
    try:
        if _converter is None:
            # keep_spaces preserves the source's spacing (``α² + √(β₁)`` rather
            # than flatlatex's default tight ``α²+√(β₁)``).
            _converter = flatlatex.converter(keep_spaces=True)
        return _BOUNDED_OP.sub(r"\1", _converter.convert(src))
    except Exception:
        # flatlatex raises LatexSyntaxError on malformed input, but any failure
        # whatsoever must degrade to the literal span, so catch broadly.
        return None


def _math_enabled() -> bool:
    raw = os.getenv("MARIM_TUI_MATH")
    if raw is None:
        return True
    return raw.strip().lower() in _TRUTHY


def math_parser_factory() -> Callable[[], MarkdownIt] | None:
    """The ``parser_factory`` for Textual's ``Markdown`` widget, or ``None``
    when math rendering is off (``MARIM_TUI_MATH=0``) or flatlatex is absent —
    ``None`` makes Textual build its stock parser, i.e. today's behavior.

    Read per widget construction (it is cheap), so the env gate takes effect
    for new messages without a restart."""
    if flatlatex is None or not _math_enabled():
        return None
    return _build_parser


def _build_parser() -> MarkdownIt:
    # Same base as Textual's default (Markdown builds MarkdownIt("gfm-like")
    # when parser_factory is None) — everything below is strictly additive.
    md = MarkdownIt("gfm-like")
    # allow_space=False rejects currency-like text ("$5 and $10": the candidate
    # closer is preceded by whitespace); double_inline catches $$..$$ used
    # display-style inside a prose line.
    dollarmath_plugin(md, allow_space=False, double_inline=True)
    md.inline.ruler.before("escape", "math_backslash_inline", _backslash_paren_inline)
    md.block.ruler.before("paragraph", "math_backslash_block", _backslash_bracket_block)
    md.core.ruler.push("math_rewrite", _rewrite_math_tokens)
    return md


# --- custom rules for the \( .. \) and \[ .. \] delimiter forms -------------
# dollarmath covers only $/$$; models (Claude, GPT) emit the backslash forms at
# least as often, so they get small purpose-built rules. Registered *before*
# the escape/paragraph rules so a complete span wins over markdown-it treating
# "\(" as an escaped paren.


def _backslash_paren_inline(state: StateInline, silent: bool) -> bool:
    src, pos = state.src, state.pos
    if not src.startswith("\\(", pos):
        return False
    end = src.find("\\)", pos + 2)
    if end < 0:
        return False
    if not silent:
        tok = state.push("math_inline_backslash", "math", 0)
        tok.content = src[pos + 2 : end]
        tok.markup = "\\("
    state.pos = end + 2
    return True


def _backslash_bracket_block(
    state: StateBlock, start_line: int, end_line: int, silent: bool
) -> bool:
    if is_code_block(state, start_line):
        return False
    begin = state.bMarks[start_line] + state.tShift[start_line]
    if not state.src.startswith("\\[", begin):
        return False
    # Scan forward for the line whose content ends with the closer. While a
    # span is still streaming (no closer yet) this fails and the text falls
    # through to the paragraph rule — the open-block re-parse converts it on a
    # later flush once the closer arrives.
    found = -1
    for line in range(start_line, end_line):
        line_src = state.src[state.bMarks[line] : state.eMarks[line]]
        if line_src.rstrip().endswith("\\]"):
            found = line
            break
    if found < 0:
        return False
    if silent:
        return True
    content = state.src[begin + 2 : state.eMarks[found]].rstrip()
    content = content[: content.rfind("\\]")].strip()
    tok = state.push("math_block_backslash", "math", 0)
    tok.block = True
    tok.content = content
    tok.markup = "\\["
    tok.map = [start_line, found + 1]
    state.line = found + 1
    return True


# --- core rule: rewrite math tokens into tokens Textual already renders -----

_INLINE_MATH = {"math_inline", "math_inline_double", "math_inline_backslash"}
_BLOCK_MATH = {"math_block", "math_block_label", "math_block_backslash"}

# token.markup -> the delimiters to restore around a span that failed to
# convert, so the fallback is byte-identical to what the model wrote.
_DELIMS = {
    "$": ("$", "$"),
    "$$": ("$$", "$$"),
    "\\(": ("\\(", "\\)"),
    "\\[": ("\\[", "\\]"),
}


def _converted_or_literal(tok: Token) -> str:
    converted = latex_to_unicode(tok.content)
    if converted is not None:
        return converted
    opener, closer = _DELIMS.get(tok.markup, ("", ""))
    return f"{opener}{tok.content}{closer}"


def _rewrite_math_tokens(state: StateCore) -> None:
    """Replace every math token with plain text/paragraph tokens carrying the
    transliteration. Textual's Markdown widget only ever sees token types it
    already knows how to render, so it needs no changes at all."""
    out: list[Token] = []
    for tok in state.tokens:
        if tok.type == "inline" and tok.children is not None:
            tok.children = [_rewrite_inline(child) for child in tok.children]
            out.append(tok)
        elif tok.type in _BLOCK_MATH:
            out.extend(_block_math_paragraph(tok))
        else:
            out.append(tok)
    state.tokens = out


def _rewrite_inline(tok: Token) -> Token:
    if tok.type not in _INLINE_MATH:
        return tok
    return Token("text", "", 0, content=_converted_or_literal(tok), level=tok.level)


def _block_math_paragraph(tok: Token) -> list[Token]:
    """Display math as a plain paragraph (a centered math widget would need
    custom Textual block classes — deliberately out of scope, see the spec)."""
    text = Token("text", "", 0, content=_converted_or_literal(tok))
    inline = Token(
        "inline", "", 0,
        content=text.content, children=[text], map=tok.map, level=tok.level + 1,
    )
    return [
        Token("paragraph_open", "p", 1, map=tok.map, level=tok.level),
        inline,
        Token("paragraph_close", "p", -1, level=tok.level),
    ]
