# Settings screen UX redesign

**Date:** 2026-06-30
**Status:** Design — approved, pending implementation plan
**Component:** `src/marim_harness/interfaces/tui/settings.py`

## Problem

The full-bleed settings screen has four rail sections — `Runtime`, `Theme`,
`MCP servers`, `Config` — and `Config` is a catch-all. Six checkboxes, two radio
sets, five integer inputs, a text input, a Save button, and four read-only info
lines are stacked in one long scroll with no grouping or hierarchy. Three concrete
UX problems follow from this:

1. **Sprawl / no organization.** Unrelated knobs (observation masking, tool-search,
   notifications, sub-agent limits, LSP) sit in one flat list. Nothing is grouped.
2. **Apply-semantics are unclear.** Runtime widgets apply immediately; the env block
   only takes effect after pressing "Save to .env" *and* relaunching. The only cue is
   one muted banner line. Worse, two settings appear twice with different semantics:
   - **Mode** is live in `Runtime` *and* "Default mode (new sessions)" is saved in `Config`.
   - **Context budget** is a read-only display in `Runtime` *and* an editable field in `Config`.
3. **Visual roughness.** `.frow` labels are 24-wide but checkboxes/radios don't align
   to that column; vertical rhythm is uneven; the single Save button + status line is
   the lone affordance for a dozen heterogeneous fields.

Out of scope: inline per-field help text / value-range hints (explicitly deferred).
Theme persistence behavior is unchanged (theme remains session-only, as today).

## Goals

- Replace the `Config` catch-all with focused, scannable topic pages.
- Make live-vs-relaunch obvious per field and eliminate the duplicated settings.
- Drop the explicit Save button in favor of auto-save on change.
- Tighten alignment and vertical rhythm.

Non-goals: changing *which* settings exist, changing env var names, persisting theme,
making the Advanced (denylist/allowlist/trust) fields editable.

## Design

### Rail structure

The rail goes from 4 sections to 7 topic pages. `Runtime` dissolves into `Session`;
`Config` is broken apart by topic. Order (live/most-used first):

```
settings
  Session            mode · model · default mode (new sessions)
  Theme              accent palette
  MCP servers        per-server enable/disable
  Context & Memory   context budget · masking knobs · proactive memory
  Tools              LSP · LSP nav tools · job tool · tool search · threshold · subagent limit
  Notifications      desktop notifications · notification events
  Advanced           denylist · allowlist · trust · config path (read-only)
```

### Field → page mapping

Every existing setting is preserved; only its home and apply-cue change.

| Page | Field | Widget | Apply |
|---|---|---|---|
| **Session** | Mode (ask/auto/plan) | RadioSet | **live** (`set_mode`) |
| **Session** | Model | label + picker button | **live** (`set_model`) |
| **Session** | Default mode (new sessions) | RadioSet | next launch (`MARIM_DEFAULT_MODE`) |
| **Theme** | Accent palette | row list | live (`app.theme`) |
| **MCP servers** | Per-server enable/disable | BoxCheckbox rows | live (`enable_server`/`disable_server`) |
| **Context & Memory** | Context budget (tokens) | Input(int) | next launch (`MARIM_MAX_CONTEXT_TOKENS`) |
| **Context & Memory** | Mask stale observations at compaction | BoxCheckbox | next launch (`MARIM_MASK_OBSERVATIONS`) |
| **Context & Memory** | Mask: keep recent returns | Input(int) | next launch (`MARIM_MASK_KEEP_RECENT`) |
| **Context & Memory** | Mask: min chars to elide | Input(int) | next launch (`MARIM_MASK_MIN_CHARS`) |
| **Context & Memory** | Proactive memory | BoxCheckbox | next launch (`MARIM_PROACTIVE_MEMORY`) |
| **Tools** | LSP | BoxCheckbox | next launch (`MARIM_LSP`) |
| **Tools** | LSP navigation tools | BoxCheckbox | next launch (`MARIM_LSP_TOOLS`) |
| **Tools** | Job tool combined | BoxCheckbox | next launch (`MARIM_JOB_TOOL_COMBINED`) |
| **Tools** | Tool search (off/auto/on) | RadioSet | next launch (`MARIM_TOOL_SEARCH`) |
| **Tools** | Tool-search threshold | Input(int) | next launch (`MARIM_TOOL_SEARCH_THRESHOLD`) |
| **Tools** | Sub-agent request limit | Input(int) | next launch (`MARIM_SUBAGENT_REQUEST_LIMIT`) |
| **Notifications** | Desktop notifications | BoxCheckbox | next launch (`MARIM_NOTIFICATIONS`) |
| **Notifications** | Notification events | Input(text) | next launch (`MARIM_NOTIFICATION_EVENTS`) |
| **Advanced** | Command denylist | Static (read-only) | — |
| **Advanced** | Command allowlist | Static (read-only) | — |
| **Advanced** | Trust project hooks | Static (read-only) | — |
| **Advanced** | Config file path | Static (read-only) | — |

**Duplication fixes:**
- Context budget collapses to the single editable field on Context & Memory; the
  read-only Runtime display is dropped.
- Mode (live) and Default mode (next launch) now sit on the same Session page, each
  tagged, so the distinction reads as intentional.

### Auto-save mechanics

No Save button anywhere. Persistence is per-field on change:

- **Checkboxes & radios** write their env var to `.env` immediately on the
  `Changed` event.
- **Integer / text Inputs** commit on **blur** (focus leaves) and on **Enter**
  (`Input.Submitted`) — never per keystroke. A malformed/empty/≤0 integer is
  **rejected**: an inline error shows next to the field, nothing is written, and the
  last stored value is retained. This is the critical correctness rule — a value
  half-typed must never clobber `.env`.
- **Live fields** (mode, model, theme, MCP) apply immediately to the session exactly
  as today and do **not** write to `.env`.

**Persistence call:** auto-save reuses `save_env_settings(values)` with a single-key
(or few-key) dict per commit, rather than the current all-at-once dict. Verified safe:
`save_env_settings` → `write_env_values` (`config/persist.py`) updates a key's line in
place and preserves every other line (comments and unmanaged keys), writes atomically,
and mirrors each value into `os.environ`. Single-key writes are therefore idempotent and
non-truncating.

**Feedback:** the changed field shows a brief muted `✓ saved · next launch` indicator
in its row; a persistent footer status echoes the last save / last error. The current
`#save-status` Static is repurposed into this footer status; the old "Saved to .env"
banner is removed.

### Live vs relaunch tagging

Each editable field row ends with a small muted tag: `live` or `next launch`. This is
the single, consistent cue that replaces the one-line banner and makes mixed pages
(Session) honest. Read-only Advanced fields carry no tag.

### Polish

- A shared label-column width applies to **all** field types (checkboxes, radios,
  inputs), so labels and controls align down a page. Today only `.frow` inputs use the
  24-wide column.
- Vertical rhythm: one blank line between field groups, no oversized gaps.
- Rail badges retained where meaningful: Session → current mode, Theme → theme name,
  MCP → server count. New pages without a single summarizing value show no badge.
- Footer hint updates to: `↑↓ section · enter edit · changes save automatically · esc close`.

## Validation / commit helper

The validation currently inlined in `_save_env` (parse int, reject ≤0, per-field error
message) is refactored into a small per-field commit path so it can run on a single
field's blur/submit. Shape:

- `_commit_int(selector, env_key, label)` — parse, reject non-positive with an inline
  error, else `save_env_settings({env_key: str(value)})` and flash the saved indicator.
- `_commit_choice(env_key, value)` — for radios/checkboxes; write and flash.
- `_commit_text(selector, env_key)` — for notification events (trimmed).

Field labels for error messages match today's wording (e.g. "Mask: keep recent returns
must be a positive integer.").

## Error handling

- Invalid integer input → inline field error, no write (as above).
- `save_env_settings` raising (write failure) → surface `Save failed: {exc}` in the
  footer status, leave the widget value as-is.
- MCP enable failure on a live toggle → unchanged from today (revert checkbox, mark
  failed).

## Testing

Extend `tests/test_settings_screen.py`:

- Each page mounts and shows its expected fields (one assertion per page).
- Toggling a checkbox/radio writes the correct env var via `save_env_settings`
  (mock and assert the single-key dict).
- Editing an integer input and blurring/submitting commits a valid value; an invalid
  value shows the field error and writes nothing.
- The duplicated settings no longer appear twice (Context budget present once; Mode and
  Default mode are distinct widgets with distinct ids).
- Live fields (mode/model) still call their harness mutations and do not write `.env`.

Run order before claiming done: `uv run ruff check src tests` → `uv run pyright` →
`uv run pytest tests/test_settings_screen.py` → full `uv run pytest`.

## Risks / notes

- **Section count.** 7 rail rows vs 4. Still comfortably within a single-column rail;
  each page is short, which is the point.
- **Auto-save surprise.** Writing on every toggle is less deliberate than an explicit
  Save. Mitigated by the per-field `✓ saved · next launch` cue and the footer status so
  the write is always visible. Integer fields commit only on blur/Enter, so partial
  edits never persist.
- **`save_env_settings` granularity.** Verified (see Persistence call) — merges in
  place, preserves other lines, atomic. No change needed there.
