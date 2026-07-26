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

import re

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
