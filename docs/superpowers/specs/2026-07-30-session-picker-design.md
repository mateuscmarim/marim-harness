# Session picker — design

**Date:** 2026-07-30
**Status:** approved (discussed in conversation 2026-07-30)

## Problem

`/sessions` (`interfaces/tui/commands.py:125-140`, `_cmd_sessions`) posts a
numbered markdown block into the chat log — one line per session, e.g.
`39. `20260712-153814` — 7 msgs, 51716 tokens, 2026-07-12 15:38`. With 100+
accumulated sessions this wraps badly inside the transcript pane (it's plain
text in a `VerticalScroll`, not a fixed-width list) and there's no way to
search, filter, or navigate it — you read the whole dump, then type a
separate `/switch <n|name>` command against a number that may have already
scrolled off-screen. There's also no way to prune old sessions short of
deleting files by hand.

Two real interactive picker screens already exist for an analogous problem —
`ModelPickerModal` and `ThinkingPickerModal`
(`interfaces/tui/model_picker.py`, `interfaces/tui/thinking_picker.py`) —
arrow-key navigable, filter-as-you-type, `ModalScreen[str | None]`. Sessions
get no equivalent. This design gives them one.

## Decision

Add a `SessionPickerModal` that mirrors `ModelPickerModal`'s structure
exactly (filter `Input` + `OptionList`, dismiss-with-id-or-None), wired the
same way (`open_session_picker()` on `HarnessApp`, `push_screen(..., callback)`
— not `push_screen_wait`, for the same reason `open_model_picker` avoids it:
`_cmd_sessions` runs off the command-dispatch path, not a Textual worker, so
`push_screen_wait` would raise `NoActiveWorker`). `/sessions` opens the picker
instead of printing text; `/switch <n|name>` is untouched, staying available
as a scriptable/typed fallback.

Session data is already fetched synchronously and cheaply
(`SessionManager.list()` parses just the JSON header, not the messages
array), so unlike the model picker there's no async-fetch/loading-state path
to build — the picker is constructed with the list already in hand.

Deletion is new. `SessionManager.delete(session_id)` already exists and does
a full teardown (session JSON, checkpoints sidecar, sub-agent transcripts,
image cache, scratchpad, checkpoint git refs — `session/store.py:496`), so no
new deletion logic is needed at the store layer. For the confirm step, this
design reuses the codebase's one existing confirmation idiom — the
double-press-within-a-window guard on quit (`app.py:706-726`,
`_QUIT_CONFIRM_WINDOW = 2.0`) — rather than introducing a new confirm-modal
pattern (the codebase has none, and deliberately removed the equivalent
guard from `/exit` in commit `efe9d0c` because an explicit command doesn't
need it — but an accidental keypress against a destructive action does, so
the guard is warranted here the way it's warranted for Ctrl+C).

Rejected alternative: a nested confirm `ModalScreen` (push a second modal
asking yes/no). Rejected because it's more code for the same guarantee, and
introduces a pattern (`ModalScreen` stacking) not used anywhere else in this
codebase, where the two-stage-keypress idiom already exists and is proven.

## Design

### 1. `SessionPickerModal` (`interfaces/tui/session_picker.py`, new file)

Structure mirrors `ModelPickerModal` (`model_picker.py:21`):

```python
class SessionPickerModal(ModalScreen[str | None]):
    CSS = """..."""  # centered box, same shape as #model-box
    BINDINGS = [("escape", "cancel", "Cancel"), ("d", "delete", "Delete")]

    def __init__(self, sessions: list[SessionInfo], active: str | None) -> None:
        ...

    def compose(self) -> ComposeResult:
        # Vertical: title Static, Input(id="session-filter"),
        # OptionList(id="session-list"), status Static (delete-armed /
        # blocked / empty-filter messages)
        ...
```

- `sessions` arrives pre-fetched and pre-sorted (newest-first, unchanged from
  today) — no `fetch`/`run_worker` plumbing like the model picker's optional
  async catalog load.
- On mount, the `OptionList` is populated from `sessions` and the option
  matching `active` is highlighted first (today's picker opens with nothing
  highlighted; this one opens on "where you are").
- `on_input_changed` filters via a new pure helper `filter_sessions(sessions,
  query) -> list[SessionInfo]`, added to `session/store.py` next to
  `SessionInfo` (substring, case-insensitive, matched against
  `SessionInfo.name`) — same shape as `workspace.catalog.filter_entries`.
- `on_option_list_option_selected` / `on_input_submitted` → `dismiss(session_id)`.
- `action_cancel` → `dismiss(None)`.

Row format, one line, fixed-width fields (extends the current
`_cmd_sessions` format with duration, which exists on `SessionInfo` today
but isn't shown anywhere):

```
<name, truncated/padded>  <msgs>msgs · <tokens>tok · <duration|—> · <updated>  [← active]
```

`tokens` renders abbreviated (e.g. `51.7k`) the way `status_bar.py` already
formats token counts, for consistency and to keep the line short. `duration`
renders `Xh Ym` / `Ym Zs` (or `—` when `duration_seconds` is `None`, which is
true for some already-persisted sessions).

**Delete flow** (`action_delete`, bound to `d`):

- First press on a highlighted, non-active session: arms it — update that
  row's label in place to prefix `⚠ press d again to delete —`, record
  `(session_id, arm_time)` on the screen.
- Second `d` press on the *same still-highlighted* row within 2.0s (mirrors
  `_QUIT_CONFIRM_WINDOW`): calls `app.harness.session.manager.delete(session_id)`
  (`SessionController.manager`, `ctrl.py:153`, guarded for `None`; delete
  itself is `SessionManager.delete`, `store.py:496`), removes the row from
  the `OptionList`, clears the armed state, posts a brief status line
  (`"Deleted <name>."`).
- Any other key, or the highlighted option changing, or the window expiring:
  clears the armed state and restores the row's plain label.
- Attempting to arm the **active** session: refused immediately, status line
  shows `"Can't delete the active session."` — no arm state is set.

### 2. `HarnessApp.open_session_picker()` (`interfaces/tui/app.py`)

Mirrors `open_model_picker` (`app.py:884-907`):

```python
async def open_session_picker(self) -> None:
    infos = self.harness.session.sessions()
    store = self.harness.session.store
    active = store.session_id if store is not None else None
    self.push_screen(SessionPickerModal(infos, active), self._on_session_chosen)

async def _on_session_chosen(self, chosen: str | None) -> None:
    if not chosen:
        return
    await self.switch_to_session_id(chosen)
```

`switch_to_session_id` (`session_view.py:529-536`) is unchanged — it's
already the exact terminal step needed (loads the session, calls
`harness.session_start("resume")`, renders the "Switched to" notice).

### 3. `commands.py`

`_cmd_sessions` (`commands.py:125-140`) shrinks to opening the picker:

```python
async def _cmd_sessions(app: HarnessApp, arg: str) -> None:
    await app.open_session_picker()
```

`_cmd_switch` / `resolve_ref` (`commands.py:107-122, 160-169`) are untouched
— `/switch <n|name>` keeps working exactly as today, independent of the
picker. The `COMMANDS` registry entry for `sessions` gets its help text
updated (was "list saved sessions", becomes something like "browse and
switch sessions (opens a picker)").

### Untouched by design

- `SessionInfo`, `SessionManager.list()`/`.delete()` — used as-is, no schema
  or store changes.
- `/switch <n|name>` and `resolve_ref` — unchanged, remains the non-picker
  path.
- Headless/CLI `--resume` (`bootstrap.py`) — unaffected; it doesn't go
  through `/sessions` at all.

## Testing

- `filter_sessions` — pure unit tests in `tests/test_session.py` (empty query
  returns all, substring match case-insensitive, no match returns empty).
- `SessionPickerModal` — Textual widget tests, new file `tests/test_session_picker.py`
  mirroring `tests/test_model_picker.py`'s structure: typing filters the `OptionList`; Enter on a
  highlighted option dismisses with its id; Escape dismisses with `None`;
  arming delete on the active session is refused and leaves the list
  unchanged; two `d` presses within the window on a non-active row removes
  it from the `OptionList` and calls through to
  `SessionManager.delete`; a single `d` press followed by a different key
  leaves the session un-deleted.
- `commands.py`: update/replace the existing `_cmd_sessions` test (if one
  posts against the old text format) to assert `open_session_picker()` is
  called instead of a chat message being posted.

No live smoke needed beyond the usual `run` skill pass to eyeball the
picker's layout/truncation at a real terminal width once implemented.

## Docs

- `CHANGELOG.md` Unreleased entry: `/sessions` now opens an interactive
  picker (filter-as-you-type, delete with a keypress) instead of printing a
  text list; `/switch` is unchanged.
