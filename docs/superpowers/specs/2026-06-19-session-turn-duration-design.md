# Session + turn duration in the TUI — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation to follow.

## Goal

Show how long things take in the TUI: a **session duration** (since this launch)
always visible in the status bar, and a **live turn timer** that ticks while a
turn runs and is stamped onto the assistant reply when it finishes.

## Context (current `interfaces/tui/app.py`)

- `_status_text()` builds the status-bar `Content`; it appends a `working…` field
  when `self._busy`.
- The status bar refreshes via `_refresh_status()`, called on events and — while
  busy — every 0.08s by the stream-flush tick (`_flush_streams`, registered with
  `set_interval(_STREAM_FLUSH_INTERVAL, ...)`; it refreshes status only `if
  self._busy`). When idle, nothing periodically refreshes it.
- `_run_turn` wraps `harness.run_turn` with `_set_busy(True)` … `finally:
  _set_busy(False)`; on `CancelledError`/`Exception` it mounts an `ErrorMessage`.
- The streamed assistant reply is `self._current_assistant` (an `AssistantMessage`,
  a `Markdown` subclass).

## Approach (all in the TUI; nothing persisted, agent/session core untouched)

Use `time.monotonic()` for all timing (immune to wall-clock changes).

### 1. Session duration (since launch)
- Set `self._session_start = time.monotonic()` in `on_mount`.
- `_status_text` adds a `session <dur>` field (coarse format), always present.
- Add an always-on `set_interval(1.0, self._refresh_status)` in `on_mount` so the
  session timer advances while idle. (During a turn the 0.08s tick already
  refreshes; the 1s tick is redundant-but-harmless then.)

### 2. Live turn timer
- Set `self._turn_start = time.monotonic()` in `_run_turn`, right before
  `_set_busy(True)`.
- In `_status_text`, when `self._busy`, render `working… <Ns>` where
  `N = int(monotonic() - self._turn_start)` (whole seconds). The existing per-frame
  refresh during a turn makes it live.

### 3. Stamp the reply on completion
- In `_run_turn`, after `harness.run_turn(...)` returns successfully (before the
  `finally`), compute `elapsed = monotonic() - self._turn_start` and mount a small
  dim `TurnMeta` line (e.g. `· 12.4s`) at the end of the log — a permanent per-turn
  record next to the reply.
- The live status timer clears automatically when `_set_busy(False)` runs.
- Cancelled (`CancelledError`) and errored turns do NOT stamp (they already mount
  an `ErrorMessage`).

### 4. Formatting — `_format_duration(seconds, *, precise=False) -> str`
- `< 60s`: `precise` → `f"{seconds:.1f}s"` (e.g. `12.4s`); else `f"{int(seconds)}s"`.
- `< 3600s`: `f"{m}m"` (drop seconds for the session/coarse view), e.g. `12m`.
- `>= 3600s`: `f"{h}h {m}m"`, e.g. `1h 5m`.
The live turn timer uses the non-precise form (whole seconds); the stamp uses
`precise=True`; the session field uses the non-precise form.

### 5. Components
- `interfaces/tui/app.py`: `_session_start`, `_turn_start`, the `session` field +
  `working… Ns` in `_status_text`, the 1s interval, `_format_duration`, the
  stamp call in `_run_turn`.
- `interfaces/tui/widgets.py`: a small `TurnMeta(Static)` widget (dim styling,
  distinct from `NoticeMessage` so it doesn't read as a system note).

## Error handling

- `_turn_start` is always set before a turn; the stamp only runs on the success
  path, so a missing/garbage value can't reach the stamp.
- `_refresh_status` already guards `NoMatches` (status bar gone during teardown);
  the 1s interval inherits that safety.

## Testing

- `_format_duration`: `5` → `5s`; `5` precise → `5.0s`; `65` → `1m`; `3725` →
  `1h 2m`.
- `_status_text` always includes a `session` field; includes `working… ` with a
  seconds count when `self._busy` and `_turn_start` is set.
- A successful (mocked `run_turn`) turn mounts exactly one `TurnMeta` line; a
  cancelled/errored turn mounts none (only `ErrorMessage`).
- The idle 1s interval is registered (and `_refresh_status` updates the session
  field over time / when `_session_start` is moved back).

## Build order

1. `_format_duration` + `TurnMeta` widget (+ tests).
2. Session timer: `_session_start`, status field, 1s interval (+ tests).
3. Live turn timer + completion stamp in `_run_turn` (+ tests).
