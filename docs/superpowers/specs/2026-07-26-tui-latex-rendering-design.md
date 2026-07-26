# TUI LaTeX Math Rendering — Design

**Date:** 2026-07-26
**Status:** Approved

## Problem

Models routinely emit LaTeX math (`$E = mc^2$`, `\(\alpha^2\)`, `\[\frac{a}{b}\]`)
in their replies. The TUI renders assistant prose through Textual's `Markdown`
widget, whose markdown-it parser has no math support, so math arrives as literal
LaTeX source. A terminal cannot typeset math, but it can display a readable
Unicode approximation (`α² + √(β₁)`, `(-b±√(b²-4ac))/(2a)`).

## Goals

- Render inline and display math as Unicode on every TUI prose surface (main
  transcript, sub-agent transcript panes, replay, intro header).
- Recognize all four delimiter forms models actually emit: `$...$`, `$$...$$`,
  `\(...\)`, `\[...\]`.
- Zero changes to the streaming/flush pipeline and its invariants.
- Degrade to today's behavior (literal LaTeX) on any failure: unparsable math,
  missing library, disabled gate.

## Non-goals

- True typeset math (image/sixel rendering) — ruled out as terminal-dependent
  and hostile to the streaming transcript.
- Math in headless `-p` output or the persisted transcript. The stored record
  stays lossless raw LaTeX; only the live TUI view transliterates.
- Math in `ThinkingWidget` (reasoning renders as plain text, not Markdown — out
  of scope by construction).
- A TUI Settings row or `/command` toggle (env gate only; can be added later).

## Approach (chosen: parser-level rewrite)

Textual's `Markdown.append` re-parses the entire still-open trailing block from
source on every flush and only advances its cursor past completed blocks. Doing
math recognition *inside the parser* therefore makes streaming self-correct for
free: an unclosed `\(x^2` renders literally, and the flush after `\)` arrives
re-parses the same open block and shows the converted math. No hold-back logic,
no rewriting of already-flushed text.

Alternatives rejected:

- **Text pre-normalization** of the delta stream — substitution is not
  prefix-stable (a late closer rewrites flushed text), forcing hold-back
  machinery into the most delicate part of the TUI.
- **Custom Textual math block widget** — prettier centered display math, but
  requires subclassing Textual's `MarkdownBlock` machinery for a cosmetic gain.
  Can be layered on later without reworking this design.

## Design

### New module: `src/marim_harness/interfaces/tui/math_markdown.py`

Three concerns in one file:

1. **`latex_to_unicode(src: str) -> str`** — the transliteration seam. Wraps a
   lazily-created, module-cached `flatlatex.converter()`. Any exception returns
   the original span verbatim **including its delimiters**, so a failed
   conversion is byte-identical to today's output. flatlatex is referenced only
   here, so the library is swappable behind this one function.

2. **Two markdown-it rules** for the backslash delimiters (dollarmath covers
   only `$`/`$$`):
   - an inline rule recognizing `\(...\)`;
   - a block rule recognizing `\[...\]` (opener at line start, scans forward
     for the closer within the block's lines).

3. **`math_parser_factory() -> Callable[[], MarkdownIt] | None`** — returns a
   factory building `MarkdownIt("gfm-like")` (the same base Textual uses) +
   `mdit_py_plugins.dollarmath` + the two custom rules + a **core rule** that
   walks the token stream and rewrites every math token (`math_inline`,
   `math_inline_double`, `math_block`, and the custom backslash token types)
   into ordinary tokens Textual already renders:
   - inline math → a `text` token whose content is `latex_to_unicode(...)`;
   - block math → `paragraph_open` / `inline` (with a single `text` child) /
     `paragraph_close`, content likewise converted.

   Returns `None` when the env gate is off **or** flatlatex is not importable;
   the caller then falls back to Textual's stock parser.

### Integration: one argument in `AssistantMessage.__init__`

`AssistantMessage` (in `interfaces/tui/widgets/messages.py`) passes
`parser_factory=math_parser_factory()` to `super().__init__()` (`None` means
Textual uses its default parser — same object semantics as today). Every prose
surface constructs `AssistantMessage`, so all of them inherit math rendering
with no further wiring. No changes to `append`/`flush`/`finalize` or any
streaming invariant.

### Gating and packaging

- `flatlatex>=0.15` is added to the existing `[tui]` optional-dependency extra
  (precedent: `textual>=0.80` lives there). LGPL-3 runtime dependency of an MIT
  project — imported, not vendored, which LGPL permits.
- `MARIM_TUI_MATH` env var, **default on**; `0`/`false` disables (same
  convention as `MARIM_FORGE`, `MARIM_SCRATCHPAD`). It is read inside
  `math_parser_factory()` at widget construction, *not* threaded through
  `HarnessBuilder`/bootstrap: this is a render-only preference of the TUI
  process, not embedding or turn-engine configuration, and `AssistantMessage`
  is constructed at several depths of the render tree where threading a flag
  adds noise for zero benefit.
- The flatlatex import is guarded even though `[tui]` declares it, so an
  embedder with a trimmed dependency set gets literal LaTeX, never a crash.

### False positives and known limits

- dollarmath's rules reject currency-like text: in `$5 and $10` the candidate
  closer is preceded by whitespace, so it stays literal.
- Fenced code blocks and inline code are structurally immune — inline/block
  math rules never fire inside them. Shell `!` passthrough output is always
  fenced (`format_transcript_block`), so `$VAR`-heavy output is safe.
- Anything flatlatex cannot parse falls back to the literal span.
- Residual worst case (e.g. `costs $5-and-$8` converting oddly):
  `MARIM_TUI_MATH=0` is the escape hatch.
- A math span containing a blank line closes the enclosing paragraph early;
  the span never converts and stays literal LaTeX. Accepted (rare).
- Math that arrives split across the `_MAX_RENDER` elision boundary of a very
  large message may stay literal. Accepted (the elided view is already lossy).

## Testing

Parser-level tests (pure, no Textual app; new `tests/test_math_markdown.py`):

- Each delimiter form converts: `$..$`, `$$..$$`, `\(..\)`, `\[..\]`.
- Representative `latex_to_unicode` conversions (Greek, sub/superscripts,
  `\frac`, `\sqrt`) and the error fallback returning the span verbatim.
- Currency text `$5 and $10` stays literal.
- Math-looking text inside a code fence and inline code stays literal.
- Gate off (`MARIM_TUI_MATH=0`) → factory is `None`; flatlatex import failure
  (simulated) → factory is `None`.

Widget-level test (Textual Pilot, alongside existing TUI tests):

- Stream `\(x^2\)` split across two `append` calls with a flush between them;
  after the second flush the rendered content contains `x²` — proving the
  open-block re-parse self-correction.

## Consequences

- One new module + one-argument change in `AssistantMessage`; no streaming
  code touched.
- One new `[tui]` dependency (flatlatex, pure Python, small).
- Display math renders as a plain (uncentered) paragraph; a dedicated math
  block widget remains a possible follow-up.
