# TUI LaTeX Math Rendering Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Render LaTeX math (`$..$`, `$$..$$`, `\(..\)`, `\[..\]`) in assistant replies as Unicode approximations (`α² + √(β₁)`) on every TUI prose surface.

**Architecture:** A new `interfaces/tui/math_markdown.py` module builds a markdown-it `parser_factory` (Textual's stock `"gfm-like"` parser + the `dollarmath` plugin + two custom rules for the backslash delimiters + a core rule rewriting math tokens into plain text/paragraph tokens via flatlatex). `AssistantMessage` passes the factory to Textual's `Markdown` — one constructor argument, zero streaming changes. Streaming self-corrects because `Markdown.append` re-parses the whole still-open trailing block each flush.

**Tech Stack:** flatlatex (new dep, `[tui]` extra), mdit-py-plugins `dollarmath` (already a transitive dep of textual), Textual `Markdown.parser_factory` (Textual ≥ 8.x, in place).

**Spec:** `docs/superpowers/specs/2026-07-26-tui-latex-rendering-design.md` — read it first.

## Global Constraints

- `requires-python = ">=3.10"` — no 3.11+ syntax (no `Self`, no bare `X | Y` in runtime positions where 3.10 chokes; `from __future__ import annotations` at module top).
- Ruff: line length 100; lint set `E,F,I,UP,B,SIM,C901` (max complexity 10; imports sorted).
- Use `uv` for everything: `uv run pytest …`, `uv run ruff …`, `uv sync`. Never bare `python`/`pip`.
- Local gate order matches CI: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- New dependency `flatlatex>=0.15` goes in the `[tui]` extra **and** the `dev` dependency group (mirroring the `textual` precedent so plain `uv sync` keeps tests working).
- Do NOT modify `AssistantMessage.append`, `.flush`, or `.finalize` — the integration is one argument in `__init__`.
- Failure always degrades to literal LaTeX: unparsable span → span verbatim *with* delimiters; flatlatex missing or `MARIM_TUI_MATH` off → stock parser.

**Verified facts** (prototyped 2026-07-26, don't re-litigate):
- `flatlatex.converter(keep_spaces=True).convert(r"\alpha^2 + \sqrt{\beta_1}")` == `"α² + √(β₁)"`; `r"\frac{a}{b}"` == `"a/b"`; `r"\frac{"` raises `LatexSyntaxError`. Converter init ≈ 0.3 ms; no macro-state leak between `convert` calls.
- The `_BOUNDED_OP` cleanup regex was prototyped against live flatlatex output: `"(∫₀)¹ x² dx"` → `"∫₀¹ x² dx"`, `"(∫₀)^∞f"` → `"∫₀^∞f"`, `"(∑ᵢ₌₁)ⁿ xᵢ"` → `"∑ᵢ₌₁ⁿ xᵢ"`, and `"(a₁)²"` is (correctly) untouched.
- `dollarmath_plugin(md, allow_space=False, double_inline=True)`: rejects `$5 and $10` (whitespace before closer), emits `math_inline` (markup `"$"`), `math_inline_double` (markup `"$$"`), `math_block` (markup `"$$"`).
- Textual 8.2.7 `Markdown.__init__` accepts `parser_factory: Callable[[], MarkdownIt] | None`; `None` → it builds `MarkdownIt("gfm-like")` itself, so our factory must start from the same base.
- `tests/conftest.py` provides a global `anyio_backend` fixture; `@pytest.mark.anyio` works in new test files.

---

### Task 1: `latex_to_unicode` seam + flatlatex dependency

**Files:**
- Modify: `pyproject.toml` (two places: `[project.optional-dependencies] tui` at ~line 46, `[dependency-groups] dev`)
- Create: `src/marim_harness/interfaces/tui/math_markdown.py`
- Create: `tests/test_math_markdown.py`

**Interfaces:**
- Produces: `latex_to_unicode(src: str) -> str | None` — Unicode transliteration of a delimiter-less LaTeX span, `None` when conversion is impossible (missing lib or parse error). Module-level `flatlatex` name (module or `None`) that Task 2's factory gate reuses.

- [ ] **Step 1: Add flatlatex to pyproject**

In `[project.optional-dependencies]`, change:

```toml
tui = ["textual>=0.80"]
```

to:

```toml
# flatlatex renders LaTeX math spans in replies as Unicode (see
# interfaces/tui/math_markdown.py); TUI-only because only the live TUI view
# transliterates — persisted transcripts keep raw LaTeX.
tui = ["textual>=0.80", "flatlatex>=0.15"]
```

In `[dependency-groups] dev`, directly under the `"textual>=0.80",` line and its comment, add:

```toml
    # The math-rendering tests need flatlatex even though it lives in the
    # optional `tui` extra, so plain `uv sync` keeps developing/CI working.
    "flatlatex>=0.15",
```

- [ ] **Step 2: Sync and verify the import works**

Run: `uv sync && uv run python -c "import flatlatex; print(flatlatex.__name__)"`
Expected: prints `flatlatex`.

- [ ] **Step 3: Write the failing tests**

Create `tests/test_math_markdown.py`:

```python
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
```

- [ ] **Step 4: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_math_markdown.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.math_markdown'`.

- [ ] **Step 5: Write the module with the seam**

Create `src/marim_harness/interfaces/tui/math_markdown.py`:

```python
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
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_math_markdown.py -v`
Expected: 6 PASS.

- [ ] **Step 7: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean. (pyright may need the `# type: ignore[assignment]` on the `flatlatex = None` fallback — it is already in the code above.)

- [ ] **Step 8: Commit**

```bash
git add pyproject.toml uv.lock src/marim_harness/interfaces/tui/math_markdown.py tests/test_math_markdown.py
git commit -m "feat(tui): latex_to_unicode transliteration seam over flatlatex"
```

---

### Task 2: math-aware parser factory (dollarmath + backslash rules + token rewrite)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/math_markdown.py` (append to Task 1's module)
- Modify: `tests/test_math_markdown.py` (append)

**Interfaces:**
- Consumes: `latex_to_unicode(src: str) -> str | None` and the module-level `flatlatex` name from Task 1.
- Produces: `math_parser_factory() -> Callable[[], MarkdownIt] | None` — returns a zero-arg factory building the math-aware parser, or `None` when flatlatex is unavailable or `MARIM_TUI_MATH` is off. This exact callable (not its result) is what Task 3 passes to Textual's `Markdown.__init__(parser_factory=...)`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_math_markdown.py`:

```python
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
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_math_markdown.py -v`
Expected: Task 1's 6 tests PASS; the new ones FAIL with `AttributeError: ... has no attribute 'math_parser_factory'`.

- [ ] **Step 3: Implement the parser factory**

Append to `src/marim_harness/interfaces/tui/math_markdown.py` (and extend the imports at the top of the file):

Top-of-file imports become:

```python
from __future__ import annotations

import os
from typing import TYPE_CHECKING, Callable

from markdown_it import MarkdownIt
from markdown_it.token import Token
from mdit_py_plugins.dollarmath import dollarmath_plugin
from mdit_py_plugins.utils import is_code_block

if TYPE_CHECKING:
    from markdown_it.rules_block import StateBlock
    from markdown_it.rules_core import StateCore
    from markdown_it.rules_inline import StateInline
```

(`markdown_it` and `mdit_py_plugins` are guaranteed present wherever textual is —
textual depends on both — so these imports are unguarded on purpose; only
`flatlatex` gets the try/except.)

Then append the implementation:

```python
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
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_math_markdown.py -v`
Expected: all PASS (6 from Task 1 + 12 new).

- [ ] **Step 5: Lint and type-check**

Run: `uv run ruff check src tests && uv run pyright`
Expected: clean. If ruff flags `C901` on `_backslash_bracket_block` (it should not — it has ~7 branches), extract the closer-scan loop into a `_find_closer_line` helper rather than adding a noqa.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/math_markdown.py tests/test_math_markdown.py
git commit -m "feat(tui): math-aware markdown parser factory (dollarmath + backslash delimiters)"
```

---

### Task 3: wire into AssistantMessage + streaming self-correction test + docs

**Files:**
- Modify: `src/marim_harness/interfaces/tui/widgets/messages.py` (imports + `AssistantMessage.__init__`, ~line 249)
- Modify: `tests/test_math_markdown.py` (append the Pilot test)
- Modify: `.env.example` (new block after the Scratchpad block, ~line 123)
- Modify: `CHANGELOG.md` (new bullet under `## [Unreleased]`)

**Interfaces:**
- Consumes: `math_parser_factory() -> Callable[[], MarkdownIt] | None` from Task 2.
- Produces: nothing new — `AssistantMessage()`'s signature is unchanged; every construction site (`stream_render.py`, `session_view.py`, `subagents/pane.py`, `app.py`) inherits math rendering with no edits.

- [ ] **Step 1: Write the failing Pilot test**

Add `import pytest` to the imports at the **top** of `tests/test_math_markdown.py`
(appending it mid-file would trip ruff's E402), so the top imports become:

```python
import pytest

from marim_harness.interfaces.tui import math_markdown
from marim_harness.interfaces.tui.math_markdown import latex_to_unicode
```

Then append the test:

```python
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
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest --no-cov tests/test_math_markdown.py::test_assistant_message_streaming_self_corrects_split_math -v`
Expected: FAIL — the assertion on `"x²"` (AssistantMessage still uses the stock parser, so the LaTeX stays literal).

- [ ] **Step 3: Pass the factory in AssistantMessage**

In `src/marim_harness/interfaces/tui/widgets/messages.py`, add to the imports (after the existing `from textual.widgets import ...` line):

```python
from ..math_markdown import math_parser_factory
```

In `AssistantMessage.__init__`, change the last line:

```python
        super().__init__("")
```

to:

```python
        # Math-aware parser (LaTeX -> Unicode; see math_markdown.py). None when
        # MARIM_TUI_MATH=0 or flatlatex is absent — Textual then builds its
        # stock parser, byte-identical to pre-math behavior.
        super().__init__("", parser_factory=math_parser_factory())
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest --no-cov tests/test_math_markdown.py -v`
Expected: all PASS.

- [ ] **Step 5: Run the neighboring widget/streaming tests**

Run: `uv run pytest --no-cov tests/test_widgets.py -v`
Expected: all PASS — in particular `test_assistant_message_streaming_never_duplicates_blocks` (the parser swap must not disturb the serialized-append invariant).

- [ ] **Step 6: Document the env var**

In `.env.example`, after the Scratchpad block (`# MARIM_SCRATCHPAD=1`, ~line 123), insert:

```bash
# --- TUI math rendering ---
# Render LaTeX math in replies ($..$, $$..$$, \(..\), \[..\]) as Unicode
# approximations (α² + √β) in the TUI. Needs flatlatex (part of the [tui]
# extra). 1 (default) or 0; the transcript on disk always keeps raw LaTeX.
# MARIM_TUI_MATH=1
```

In `CHANGELOG.md`, add the first bullet under `## [Unreleased]`:

```markdown
- The TUI renders LaTeX math in replies (`$..$`, `$$..$$`, `\(..\)`, `\[..\]`)
  as Unicode approximations (`α² + √(β₁)`, `(-b±√(b²-4ac))/(2a)`) on every
  prose surface, including sub-agent transcripts. Streaming-safe by design
  (the parser converts a span once its closer arrives), falls back to literal
  LaTeX on anything unparsable, `MARIM_TUI_MATH=0` disables. flatlatex joins
  the `[tui]` extra.
```

- [ ] **Step 7: Full local gate (CI order)**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: all three clean/green (full suite, coverage on).

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/messages.py tests/test_math_markdown.py .env.example CHANGELOG.md
git commit -m "feat(tui): render LaTeX math as Unicode in assistant replies"
```

- [ ] **Step 9: Live smoke (manual, cheap model)**

Per the standing rule (no paid models without approval; mimo is the pre-approved exception): run
`MARIM_PROVIDER=openrouter MARIM_MODEL=xiaomi/mimo-v2.5 uv run marim` in a tmux pane,
ask "state the quadratic formula using display math", and eyeball that the reply shows
Unicode math, not raw LaTeX. Then `MARIM_TUI_MATH=0 uv run marim` once to confirm the
gate restores literal LaTeX.
