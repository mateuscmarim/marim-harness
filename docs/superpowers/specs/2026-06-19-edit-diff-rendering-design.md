# Inline diff rendering for file-edit tools + Ctrl+O reveal-all — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation to follow.

## Goal

Make `edit_file` tool calls render a readable colored diff inline in the TUI
(instead of a raw `edits=[Edit(old_string=…, new_string=…)]` repr), auto-expanded
but capped so a big edit can't flood the log, and add a global `Ctrl+O` toggle that
reveals every tool output in full. `write_file` shows its new content
syntax-highlighted. Strictly a TUI-rendering change — the tools' return values and
what the model sees are unchanged.

## Context (current `interfaces/tui/widgets.py`, `app.py`)

- `ToolCallWidget(Collapsible)` renders one tool call: `_summary()` is the title
  (`glyph tool_name(arg_preview)`); `_render_body()` returns a Rich `RenderableType`
  (arg repr lines, then the result; `read_file` results are syntax-highlighted via
  `_LEXERS`). Starts `collapsed=True`. Built as `Static(..., markup=False)` because
  tool args/results are untrusted text.
- Consecutive tool calls fold into a `ToolGroupWidget(Collapsible)` (collapsed),
  whose children are the `ToolCallWidget`s; a lone call stays a bare widget.
- Tool args reach the widget via `part.args_as_dict()`, so `args["edits"]` is a list
  of dicts `{"old_string": …, "new_string": …}` and `args["content"]` is a str.
- `app.py` has `BINDINGS` (e.g. `("ctrl+t", "cycle_mode", …)`) and `action_*`
  methods. `edit_file`/`write_file` return `"applied N edits"` / `"wrote X bytes"`.

## Approach (TUI-only; no tool or model-context changes)

### 1. Diff helper (`widgets.py`)
```
_DIFF_CAP = 20

def render_edit_diff(edits, *, cap):  # cap: int | None
    -> tuple[Text, int, int]   # (renderable, added, removed)
```
- For each edit, emit its `old_string` lines as red `- <line>` and `new_string`
  lines as green `+ <line>`; a blank line separates edits.
- `added` / `removed` = total `+` / `-` line counts across all edits.
- If `cap` is an int and the rendered diff exceeds `cap` lines, keep the first `cap`
  lines and append a dim footer `… +M more lines (ctrl+o)` (M = lines hidden).
  `cap=None` ⇒ no truncation.
- Pure function, no file reads. Built as a Rich `Text` with `"green"`/`"red"`
  styles (not Textual markup), preserving the `markup=False` safety.

### 2. `ToolCallWidget`
- Add `self.reveal: bool = False` and `set_reveal(value: bool) -> None` — sets
  `self.reveal`, re-renders the body (`self._body.update(self._render_body())`), and
  when revealing also expands (`self.collapsed = False`).
- `_render_body()` special-cases:
  - `edit_file` → `render_edit_diff(self.args.get("edits", []), cap=None if
    self.reveal else _DIFF_CAP)` renderable, followed by the result line.
  - `write_file` → its `content` arg syntax-highlighted by file extension (reuse the
    existing `_LEXERS` path used for `read_file`), followed by the result line.
  - otherwise → unchanged.
- `_summary()` for `edit_file` appends `  +N −M` (from the diff counts).
- `edit_file` widgets are constructed `collapsed=False` (auto-expanded inline);
  every other tool stays `collapsed=True`. A grouped edit therefore renders its
  diff already-open the moment its `ToolGroupWidget` is expanded.

### 3. Global `Ctrl+O` reveal-all (`app.py`)
- Add `("ctrl+o", "toggle_outputs", "Show all output")` to `BINDINGS` and
  `self._show_all_output = False` in `__init__`.
- `action_toggle_outputs()`: flip `self._show_all_output`; then
  - for every `ToolGroupWidget`: `collapsed = not self._show_all_output` (open groups
    when revealing, re-collapse when toggling off);
  - for every `ToolCallWidget`: `set_reveal(self._show_all_output)` (uncaps edit
    diffs and expands when revealing; re-caps and restores default collapse when off
    — `edit_file` back to expanded, others back to collapsed).
- Uses `self.query(ToolGroupWidget)` / `self.query(ToolCallWidget)` (recursive, so
  grouped widgets are covered).

## Boundary / non-goals

- The `edit_file`/`write_file` return strings and the model's context are unchanged;
  headless is unaffected. This is purely how the human-facing log renders.
- No true unified diff with file context (per-edit `-`/`+` of the old/new strings,
  which needs no file reads). A context-aware diff is out of scope.
- The inline cap applies only to `edit_file` diffs; other tool bodies render as today
  (full content when expanded). `Ctrl+O` reveals them by expanding their collapsibles.

## Error handling

- `edits` missing/empty or malformed (not a list of dicts) → `render_edit_diff`
  yields an empty diff (counts 0/0); the widget still shows the result line.
- A `content` arg that isn't valid source for a lexer → fall back to plain text
  (same `try/except` the `read_file` highlighting already uses).

## Testing

- `render_edit_diff`:
  - single edit: red `- old` and green `+ new` lines present; counts correct.
  - multi-edit: separated, counts summed.
  - multiline old/new: each line gets its own `-`/`+`.
  - cap: a >cap diff truncates to `cap` lines and ends with the `… +M more lines`
    footer; `cap=None` shows everything.
- `ToolCallWidget`:
  - `edit_file` body is the diff (contains `- ` / `+ ` lines, not `edits=[`), and the
    widget is `collapsed=False`; title contains `+N −M`.
  - `write_file` body is highlighted content (not a `content:` repr).
  - `set_reveal(True)` re-renders uncapped and expands; `set_reveal(False)` restores.
- `app.action_toggle_outputs`: toggling sets `reveal` on tool widgets and opens
  groups; toggling again restores defaults.

## Build order

1. `render_edit_diff` helper + `_DIFF_CAP` (+ tests).
2. `ToolCallWidget`: `reveal`/`set_reveal`, `_render_body` cases (edit diff +
   write_file highlight), `_summary` stat, `edit_file` `collapsed=False` (+ tests).
3. `app.py`: `Ctrl+O` binding, `_show_all_output`, `action_toggle_outputs` (+ tests).
