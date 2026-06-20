# Compaction summary widget — design

**Date:** 2026-06-20
**Status:** Approved (design); implementation to follow.

## Goal

When a conversation is compacted, the model-written summary is stored in history
as a user-prompt message prefixed with `[Summary of earlier conversation,
condensed to save context]`. Today the TUI renders it as a plain `UserMessage`,
so on resume it is indistinguishable from something the user typed, and during a
live compaction the summary is not shown at all (only a `compacted history: N → M
messages` notice). Make the summary legible: render it as a distinct, labeled,
collapsible block — on resume and when compaction happens live.

Strictly a TUI/affordance change — compaction logic and what the model sees are
unchanged.

## Context

- `compaction.py` `_summary_message(summary)` builds the synthetic
  `ModelRequest(UserPromptPart(content="[Summary …]\n\n{summary}"))`. The prefix
  literal lives inline at one call site.
- `session_view.replay_history` renders each restored `UserPromptPart` as
  `UserMessage(strip_turn_context(text))`. `strip_turn_context` leaves the
  summary untouched (it starts with `[Summary…`, not `<turn-context>`).
- `app._on_compact(before, after)` replaces a live "compacting…" notice with a
  `NoticeMessage("compacted history: N → M messages")`. The older messages stay
  rendered in the log; only the underlying history is condensed.
- Widgets that are untrusted model/user text render `markup=False` (e.g.
  `UserMessage`). Tool/sub-agent widgets are `Collapsible`s, collapsed by default.

## Approach (TUI-only)

### 1. Shared summary marker (`compaction.py`)
- Module constant `SUMMARY_PREFIX = "[Summary of earlier conversation, condensed
  to save context]"`. `_summary_message` builds its content from it
  (`f"{SUMMARY_PREFIX}\n\n{summary}"`), so the literal exists once.
- `summary_text(content) -> str | None`: if `content` is a `str` starting with
  `SUMMARY_PREFIX`, return the body after the leading `"\n\n"` (stripped); else
  `None`. The single source of truth for detecting/parsing a summary message.
  Pure, no imports beyond the constant.

### 2. `SummaryWidget` (`widgets.py`)
- A `Collapsible` titled `≡ Conversation summary`, **collapsed by default**, whose
  body is the summary text in a `Static(markup=False)` (model text is untrusted,
  same handling as `UserMessage`). The title is a literal `Content` to bypass
  markup parsing, matching the other collapsibles in the module.
- Constructed `SummaryWidget(summary_body: str)`.

### 3. Resume replay (`session_view.py`)
- In `replay_history`, in the `UserPromptPart` branch, after computing the string
  `text`: if `summary_text(text)` is not `None`, mount `SummaryWidget(body)` and
  reset the tool-run grouping (`group = solo = None`), instead of a `UserMessage`.
  Otherwise unchanged. Only the `str`-content path can be a summary (summary
  messages always carry `str` content).

### 4. Live compaction (`app.py`)
- In `_on_compact`, after mounting the `compacted history` notice, find the
  current summary in `self.harness.session.history` via `summary_text` (scan for a
  `UserPromptPart` whose content parses to a summary; take the last) and mount a
  collapsed `SummaryWidget` for it so the just-created summary is visible
  immediately. If none is found (plain-truncation fallback), mount nothing extra.

## Boundary / non-goals

- Compaction logic, summary quality, and the model's context are unchanged.
- No `/summary` command (considered, not chosen).
- The summary body is shown verbatim as plain text (no markdown rendering), for
  the same safety reason `UserMessage` uses `markup=False`.

## Error handling

- `summary_text` returns `None` for non-`str` content, missing prefix, or a
  prefix-only message with no body (returns an empty string only when a body
  exists). A `None` result means "render as a normal message".
- The live path mounts a summary widget only when one is found; a failed/truncated
  compaction (no summary message) leaves just the notice.

## Testing

- `summary_text`: returns the body for a real summary message; `None` for a normal
  prompt, for non-`str` content, and for a string lacking the prefix.
- `SummaryWidget`: is a `Collapsible`, `collapsed` by default; the title contains
  "Conversation summary"; the body shows the summary text, not the `[Summary…]`
  prefix.
- `replay_history`: a history containing a summary message mounts a
  `SummaryWidget` (and no `UserMessage` for it); a normal prompt still mounts a
  `UserMessage`.

## Build order

1. `SUMMARY_PREFIX` + `summary_text` (+ tests), `_summary_message` uses it.
2. `SummaryWidget` (+ tests).
3. `replay_history` summary branch (+ test).
4. `app._on_compact` live mount.
