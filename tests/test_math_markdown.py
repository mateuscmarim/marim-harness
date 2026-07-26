"""Math rendering for the TUI: LaTeX -> Unicode transliteration seam and the
math-aware markdown parser factory (see interfaces/tui/math_markdown.py)."""

from marim_harness.interfaces.tui import math_markdown
from marim_harness.interfaces.tui.math_markdown import latex_to_unicode


def test_latex_to_unicode_converts_greek_scripts_and_roots():
    assert latex_to_unicode(r"\alpha^2 + \sqrt{\beta_1}") == "α² + √(β₁)"


def test_latex_to_unicode_converts_fractions():
    assert latex_to_unicode(r"\frac{a}{b}") == "a/b"


def test_latex_to_unicode_unwraps_bounded_big_operators():
    # flatlatex parenthesizes a sub-scripted big operator before applying the
    # superscript — "(∫₀)¹" — which reads worse than it needs to. The seam
    # strips those parens for big operators only.
    assert latex_to_unicode(r"\int_0^1 x^2 dx") == "∫₀¹ x² dx"
    assert latex_to_unicode(r"\sum_{i=1}^{n} x_i") == "∑ᵢ₌₁ⁿ xᵢ"
    # The superscript may also be a caret form when no Unicode char exists.
    assert latex_to_unicode(r"\int_0^\infty f") == "∫₀^∞f"


def test_latex_to_unicode_keeps_ordinary_parenthesized_powers():
    # "(a_1)^2" is real grouping, not a bounded operator — must survive.
    assert latex_to_unicode(r"(a_1)^2") == "(a₁)²"


def test_latex_to_unicode_returns_none_on_unparsable_input():
    # Unbalanced brace: flatlatex raises LatexSyntaxError; the seam maps any
    # failure to None so callers can fall back to the literal span.
    assert latex_to_unicode(r"\frac{") is None


def test_latex_to_unicode_returns_none_without_flatlatex(monkeypatch):
    # A trimmed embedder install without the [tui] extra's flatlatex must
    # degrade, never crash.
    monkeypatch.setattr(math_markdown, "flatlatex", None)
    assert latex_to_unicode(r"x^2") is None
