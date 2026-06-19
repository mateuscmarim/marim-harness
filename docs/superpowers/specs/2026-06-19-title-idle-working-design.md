# Session name in terminal title + idle/working indicator — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation to follow.

## Goal

Move the session name out of the status bar and into the terminal title, prefixed
with an idle/working indicator, so the active session and whether the agent is
working are visible from the terminal tab/window — even when it isn't focused.

## Context (current `interfaces/tui/app.py`)

- `compose()` yields a `Header`. `on_mount` sets `self.title = "marim-harness"` and
  `self.sub_title = str(workspace_root)`. In Textual these reactives drive BOTH the
  in-app `Header` and the OS terminal title.
- `_status_text()` builds the status bar; its `head` field is the session name +
  mode (`name · mode`) or just `mode`.
- `self._busy` is the working flag (set by `_set_busy`).
- Session-name source: `self.harness.session.session_name` (may be None).
- Session state changes flow through `_set_busy` (busy toggle), `_on_rename`
  (auto-naming), and `_render_session` (new/switch/clear, which calls
  `_refresh_status`).

## Approach (all in `interfaces/tui/app.py`; no new widget, no core changes)

### 1. `_refresh_title()`
```python
def _refresh_title(self) -> None:
    mark = "●" if self._busy else "○"
    name = self.harness.session.session_name or "marim-harness"
    self.title = f"{mark} {name}"
```
`sub_title` continues to hold the workspace path (set in `on_mount`). Effect:
terminal title and Header both read `○ my-session — /path` (idle) or
`● my-session — …` (working). Unnamed/new sessions fall back to `marim-harness`.

### 2. Call sites
Call `_refresh_title()` from:
- `on_mount` — after setting `sub_title` (initial title).
- `_set_busy()` — flips ● ↔ ○ in lockstep with turn start/end.
- `_on_rename()` — picks up an auto-generated session name.
- `_render_session()` — the shared new/switch/clear path (covers session changes).

Setting `self.title` to an unchanged value is a no-op (Textual reactive), so
calling it from `_set_busy` (which also fires on the per-turn boundary) is safe.

### 3. Status bar
In `_status_text`, drop the session-name segment from `head`: it becomes just the
mode (`Content(mode)`). The model-generated-name safety comment goes away with the
segment. The existing `working… Ns` field stays, so in-app working state is still
shown. All other status fields (ctx, tokens, cost, session duration) are unchanged.

## Error handling

- `session_name` is `None` for unnamed sessions → fall back to `"marim-harness"`.
- The session name is model-generated/untrusted, but a plain `self.title = f"..."`
  string assignment is not parsed as markup (unlike the status bar's `Content`
  markup path), so no escaping is needed for the title.

## Testing

- `_refresh_title`: with a named session, `app.title == "○ <name>"` when idle and
  `"● <name>"` when `self._busy` is True (then `_refresh_title()`).
- An unnamed session → `app.title == "○ marim-harness"`.
- After `switch_to_session_id` / rename, `app.title` reflects the new name.
- The status bar no longer contains the session name (its head is just the mode),
  while `app.title` does contain the name.
- `sub_title` still holds the workspace path.

## Build order

1. Add `_refresh_title()`; call it from `on_mount`, `_set_busy`, `_on_rename`,
   `_render_session`.
2. Drop the session-name segment from the status-bar `head`.
3. Tests.
