# Tool & tool-group rendering redesign

**Date:** 2026-06-24
**Status:** Approved design — pending implementation plan

## Problem

The TUI renders tool calls inconsistently. The screenshot that prompted this shows
some tools reading cleanly (`read_file · .marim/test_output.txt`) while others spill
raw Python-repr (`wait_for_job(id='job-6', timeout=600)`,
`bash(command='…', background=True)`).

Root cause: there are **two divergent tool-summary code paths**, and the main one
branches on *argument count* rather than meaning.

- **Subagent cards** (`widgets/subagent.py` `humanize_tool` + `widgets/format.py`
  `tool_preview`) humanize: `Read`/`Bash`/`Grep` + the first *meaningful* arg.
- **`ToolCallWidget._summary_body`** (`widgets/tools.py:77`) does:

  ```python
  if len(items) == 1:
      return f"{self.tool_name} · {_clip(str(items[0][1]))}"      # clean
  return f"{self.tool_name}({_clip(', '.join(f'{k}={v!r}' …))})"  # raw repr
  ```

  So `read_file` is clean only because it has one arg; `bash`/`wait_for_job` fall to
  raw repr the moment a second arg (`background`, `timeout`) appears — even though
  `command`/`id` is the obviously salient value.

Secondary issues confirmed in the same render path:

- The pending glyph is `·` — the *same* character as the clean-style separator, so
  `·` is overloaded (pending-status **and** separator **and** group breakdown joiner).
- Secondary args are pure noise (`timeout=600` is almost always the default; the
  `repr` quotes add clutter).
- Tool widgets (`Collapsible`) carry Textual's faint default title-bar background
  band; subagent cards (plain `Vertical`) are transparent with a `border-left` rail.
  The two widget families look different.
- Groups (`ToolGroupWidget`) stay expanded after finishing, so a long run sprawls.

## Goal

One tool-summary system, shared by every renderer, producing a consistent,
humanized line and a coherent visual language across tool rows, tool groups, and
subagent cards.

## Design

### 1. One summary helper (single source of truth)

New module `interfaces/tui/widgets/tool_summary.py` exposing:

```python
@dataclass(frozen=True)
class ToolSummary:
    label: str          # humanized verb: Read, Bash, Grep, Wait, Edit, …
    target: str         # the salient value (command / path / pattern / id), clipped
    badges: list[str]   # compact dim chips: ["bg"], or [] when none

def summarize(tool_name: str, args: dict) -> ToolSummary: ...
```

Every renderer builds its `Content` from a `ToolSummary` instead of formatting args
itself:

- `ToolCallWidget._summary_body` (`widgets/tools.py`) — the arg-count branch at
  `tools.py:87` is **deleted** and replaced with a `summarize()` call.
- `ToolGroupWidget._summary` (`widgets/tools.py`) — humanizes child names via the
  same label map.
- The subagent card `↳` activity line (`widgets/subagent.py`) — switches to
  `summarize()` for its label + target.

`humanize_tool` (subagent.py) and `tool_preview` (format.py) are absorbed into
`tool_summary.py` (or re-exported from it) so there is exactly one label map and one
target-extraction rule. `_clip` stays in `format.py` and is reused.

### 2. Descriptor registry

A small table in `tool_summary.py` keyed by tool name. Each entry declares the
**target** arg and a **badge** rule; everything else is dropped. Special cases stay
special.

| Tool          | label | target arg | badges / special                          |
|---------------|-------|------------|-------------------------------------------|
| `read_file`   | Read  | `path`     | —                                         |
| `write_file`  | Write | `path`     | —                                         |
| `edit_file`   | Edit  | `path`     | `+N -M` diff stat (keep existing logic)   |
| `bash`        | Bash  | `command`  | `bg` when `background=True`                |
| `grep`        | Grep  | `pattern`  | `in <path>` when a path is given          |
| `glob`        | Glob  | `pattern`  | —                                         |
| `wait_for_job`| Wait  | `id`       | `timeout` dropped (default noise)         |
| `spawn_agent` | Spawn | task/title | (rendered as a card, not a row)           |

**Generic fallback** (unknown tools): `label = humanize(name)` (title-case, `_`→space),
`target =` first meaningful arg (the existing `tool_preview` rule), `badges = []`.
This guarantees no tool ever falls back to raw `key=value` repr again.

**Clipping:** targets clip via `_clip` (cap 100 for the main row). `bash` commands
clip **middle-out** so the tail of a pipeline survives
(`uv run pytest … | tail -1`) — a small `_clip_middle` helper in `format.py`.

**Markup safety:** targets are untrusted, so they are always rendered as literal
`Content` (never `Content.from_markup`), preserving today's guarantee.

### 3. Visual system

- **Status glyph is the only status signal**, and carries the color:
  spinner frame (pending) / `✓` (done) / `✕` (denied) / `✗` red (failed). Switching
  pending from `·` to the spinner the subagent cards already animate frees `·` to
  mean "separator" exclusively. The spinner uses the existing `set_interval` tick
  pattern from the subagent card.
- **Weight/colour:** label normal weight, target muted (`$text-muted`), badges dim.
  Structure comes from the rail + the glyph, not from heavy styling.
- **Background:** tool rows go **transparent to match the subagent cards** — strip
  the Collapsible default band, keep only the `border-left` rail as the structural
  cue. This unifies the two widget families and avoids nested bands inside groups.
  (One-line CSS change; reversible.)

### 4. Grouping behaviour

`ToolGroupWidget`:

- Header humanizes names with multipliers: `≡ 3 tools · Read ×2 · Grep`.
- **While running:** expanded, showing live child rows (each with its own spinner).
- **On finish** (every child reached a terminal status): auto-collapse to a single
  summary line appending aggregate wall-clock — `▶ ≡ 3 tools · Read ×2 · Grep · 1.4s`.
  The user can click to re-expand.
- A lone call still mounts bare (no group-of-one); the existing `add_tool_to_run`
  promotion logic in `stream_render.py` is unchanged structurally — only the header
  rendering and the finish-collapse are added.

### 5. Edge cases

- Failed tool: red `✗` + literal body (unchanged).
- Empty / no-meaningful-args tool: label only, no `· target`.
- Gated tool re-emitting its call event post-approval: reuses the mounted widget
  (existing `tool_widgets` dedup in `stream_render.py`) — unaffected.

## Testing

- Pure helpers tested directly in `tests/test_widgets.py`: `summarize()` for each
  representative tool (`bash`+bg, `wait_for_job` timeout-dropped, `edit_file` diff,
  `grep` with path, unknown-tool generic fallback), plus `_clip_middle`.
- Widget tests assert the rendered header `Content` shape for those tools.
- Group test: rows visible while running; folds to one line with duration when all
  children finish; re-expands on toggle.
- Existing markup-safety test extended to confirm untrusted targets are literal.
- CI order: `ruff check` → `pyright` → `pytest` (coverage ≥ 90%).

## Out of scope

- Changing *which* tools group (still purely consecutive-arrival based).
- Changing tool behavior or signatures.
- Reworking the subagent card layout (only its `↳` line switches to `summarize()`).
