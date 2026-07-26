"""Math rendering for the TUI: LaTeX -> Unicode transliteration seam and the
math-aware markdown parser factory (see interfaces/tui/math_markdown.py)."""

import pytest

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


def _parse(text: str):
    factory = math_markdown.math_parser_factory()
    assert factory is not None, "factory must be available in the test env"
    return factory().parse(text)


def _text_contents(tokens) -> list[str]:
    """Flatten to the text/fence token contents a reader would see."""
    out: list[str] = []
    for tok in tokens:
        if tok.type in ("text", "fence", "code_inline"):
            out.append(tok.content)
        if tok.children:
            out.extend(_text_contents(tok.children))
    return out


def test_inline_dollar_math_converts():
    texts = _text_contents(_parse("Energy: $E = mc^2$ done"))
    assert "E = mc²" in texts
    assert "Energy: " in texts  # surrounding prose intact


def test_display_dollar_math_converts_to_paragraph():
    tokens = _parse("$$\n\\frac{a}{b}\n$$")
    assert [t.type for t in tokens] == ["paragraph_open", "inline", "paragraph_close"]
    assert "a/b" in _text_contents(tokens)


def test_backslash_paren_inline_converts():
    texts = _text_contents(_parse(r"so \(\alpha^2 + \beta_1\) holds"))
    assert "α² + β₁" in texts


def test_backslash_bracket_block_converts():
    assert "x²" in _text_contents(_parse("\\[\nx^2\n\\]"))
    assert "x²" in _text_contents(_parse(r"\[ x^2 \]"))  # single-line form


def test_double_dollar_inline_converts():
    assert "yᵢ" in _text_contents(_parse("foo $$y_i$$ bar"))


def test_currency_stays_literal():
    texts = _text_contents(_parse("it costs $5 and $10 total"))
    assert texts == ["it costs $5 and $10 total"]


def test_code_fence_and_inline_code_untouched():
    assert _text_contents(_parse("```\n$E=mc^2$\n```")) == ["$E=mc^2$\n"]
    assert "echo $HOME" in _text_contents(_parse("run `echo $HOME` now"))


def test_unparsable_math_falls_back_to_literal_span_with_delimiters():
    class _Raising:
        def convert(self, src: str) -> str:
            raise ValueError("boom")

    # Force the cached converter to fail so the rewrite rule takes the
    # literal-fallback path; delimiters must be restored around the span.
    import unittest.mock as mock

    with mock.patch.object(math_markdown, "_converter", _Raising()):
        texts = _text_contents(_parse(r"before $\frac{$ after"))
    assert any(r"$\frac{$" in t for t in texts)


def test_factory_none_when_gate_off(monkeypatch):
    monkeypatch.setenv("MARIM_TUI_MATH", "0")
    assert math_markdown.math_parser_factory() is None


def test_factory_none_without_flatlatex(monkeypatch):
    monkeypatch.setattr(math_markdown, "flatlatex", None)
    assert math_markdown.math_parser_factory() is None


def test_factory_on_by_default(monkeypatch):
    monkeypatch.delenv("MARIM_TUI_MATH", raising=False)
    assert math_markdown.math_parser_factory() is not None


@pytest.mark.anyio
async def test_assistant_message_streaming_self_corrects_split_math():
    """A \\(..\\) span split across two appends must convert once the closer
    arrives: Markdown.append re-parses the still-open trailing block from
    source each flush, so no hold-back logic exists anywhere — this test is
    the proof that the parser-level design actually self-corrects."""
    from textual.app import App, ComposeResult
    from textual.widgets._markdown import MarkdownBlock

    from marim_harness.interfaces.tui.widgets import AssistantMessage

    class _App(App):
        def compose(self) -> ComposeResult:
            yield AssistantMessage()

    app = _App()
    async with app.run_test() as pilot:
        msg = app.query_one(AssistantMessage)
        msg.append("value \\(x^2")   # opener only — renders literally for now
        msg.flush()
        await pilot.pause()
        msg.append("\\) end")        # closer arrives in a later delta
        for _ in range(20):          # drain like the permanent flush interval
            msg.flush()
            await pilot.pause()
        rendered = " ".join(
            block._content.plain for block in msg.query(MarkdownBlock)
        )
        assert "x²" in rendered
        assert "\\(" not in rendered
