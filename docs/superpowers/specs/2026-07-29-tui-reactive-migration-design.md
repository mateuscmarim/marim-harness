# TUI Reactive Migration Design

**Date:** 2026-07-29  
**Status:** Approved  
**Scope:** Full TUI migration from manual refresh to Textual reactive patterns  
**Approach:** Parallel incremental — extract widgets from HarnessApp while migrating to reactive, one widget at a time

## Context

The marim-harness TUI (Textual 8.2.7) has only **2 reactive declarations** in the entire codebase (one in `settings.py`, one inherited from `App.theme`). State management relies on ~30 mutable instance variables with **19 manual `refresh_status()`** calls and **12+ manual `_append_log()`** calls scattered across 5 files. This creates fragile state lifecycles (the 3-method compact notice), tight coupling between widgets, and a HarnessApp that has grown to 1271 lines.

The codebase review (2026-07-29) rated the TUI at **7.5/10** — the lowest of any subsystem — citing weak reactive adoption, size creep, and the plain-class `SubAgentsScreen` as key issues.

## Goal

Migrate the TUI to Textual's reactive state management, extracting widgets from HarnessApp in the process. Each extraction is an independent, reviewable PR with its own behavioral tests.

## Approach: Parallel Incremental

Extract one widget at a time from HarnessApp, migrating it to reactive as you extract. Start with the easiest wins (StatusBar, CompactNotice) and work toward the harder ones (StreamRenderer, SubAgentsScreen). Each step *reduces* HarnessApp's size and *creates* a reactive reference point.

**Testing strategy:** Incremental per-widget. Write behavioral tests for each widget *before* extracting it (capture baselines with the current manual-refresh approach), then migrate and verify the same tests pass with reactive.

---

## Section 1: StatusBar Widget

### Current State

Status rendering is split between `StatusPresenter` (status.py, ~200 lines of state + formatting) and `HarnessApp.refresh_status()` (called 19 times across 5 files). The presenter holds `busy`, `spin`, `session_start`, `turn_start`, memoized `_ctx_tokens`/`_cost`, and a `status_text()` method. Every caller manually calls `refresh_status()` to re-render.

### Proposed Design

Extract a `StatusBar` Textual widget that owns its state and renders automatically.

```
StatusBar (new widget, mounts in app's #status dock)
├── reactive[bool] busy           # spinner on/off
├── reactive[float] turn_start    # elapsed timer drives periodic repaint
├── reactive[int] live_run_tokens # token count from StreamRenderer
├── reactive[float] last_ttft     # time-to-first-token
├── reactive[str] model_name      # current model display
├── reactive[str] mode            # auto/ask/plan badge
└── compute_status_text() → str   # auto-called on any reactive change
```

### Key Changes

- `HarnessApp` sets `self.query_one(StatusBar).busy = True` instead of calling `refresh_status()`
- The 19 `refresh_status()` call sites across 5 files become direct reactive assignments
- `StatusPresenter` becomes a thin formatting helper (or is absorbed into `StatusBar`)
- The `_turn_starting` latch stays on `HarnessApp` (it's a race guard, not render state)
- A `set_interval(1, ...)` in `StatusBar` handles the elapsed-timer tick

### State That Moves Out of HarnessApp

- `self._status_busy` → `StatusBar.busy`
- `self._model_name` → `StatusBar.model_name`
- `self._mode` → `StatusBar.mode`
- `self._live_run_tokens` → `StatusBar.live_run_tokens`
- `self._last_ttft` → `StatusBar.last_ttft`

---

## Section 2: CompactNotice Widget

### Current State

The compact lifecycle is a 3-method fragile chain in HarnessApp:
- `_on_compact_start` → mounts an `NoticeMessage`, stores ref in `self._compacting_notice`
- `_on_compact` → removes it, replaces with final message
- `clear_compacting_notice` → try/except/remove dance in `finally` blocks

If `maybe_compact` raises between start and compact, the notice gets stranded.

### Proposed Design

A self-contained `CompactNotice` widget that manages its own lifecycle.

```
CompactNotice (new widget, mounts in #status dock, hidden by default)
├── reactive[bool] compacting   # True → show spinner + "Compacting…", False → hide
├── reactive[bool] done         # True briefly → show green checkmark, then auto-hide
├── reactive[str] error_msg     # non-empty → show red error, then auto-hide
├── watch_compacting()          # mounts/unmounts the spinner
├── watch_done()                # shows checkmark, calls set_timer(2s, self.hide)
└── watch_error_msg()           # shows error, calls set_timer(5s, self.hide)
```

### Key Changes

- HarnessApp sets `self.query_one(CompactNotice).compacting = True` in `_on_compact_start`
- Sets `.compacting = False` + `.done = True` in `_on_compact` on success
- Sets `.compacting = False` + `.error_msg = str(e)` on failure
- The fragile `clear_compacting_notice` try/except is **deleted entirely**
- The `_compacting_notice` ref is deleted
- On session teardown, set `.compacting = False` (watcher hides it)

### Why This Is Better

The state machine is entirely within the widget. No dangling refs, no manual cleanup. A reactive `compacting` boolean can't be "stranded" — setting it to `False` always hides the notice.

---

## Section 3: QueueDisplay

### Current State

`TurnQueue` (queue.py) manages pending messages with a `.paused` bool and a `.items` list. Changes are propagated via `app._render_queue()` → `QueuePanel.show_queue()` (a `SidePanel` subclass in widgets/panels.py). The queue panel is composed in the right-hand layout but rendering is driven by explicit calls.

### Proposed Design

A `QueueDisplay` widget that owns the queue rendering.

```
QueueDisplay (new widget, mounts below the prompt)
├── reactive[list] items         # bound to TurnQueue.items
├── reactive[bool] paused        # bound to TurnQueue.paused
├── watch_items()                # re-render list items
├── watch_paused()               # show/hide pause badge
├── compose() → ListView         # renders items with edit/remove bindings
└── on_list_item_selected()      # edit queued message
```

### Key Changes

- `TurnQueue` keeps its internal list logic, but `QueueDisplay` observes it via reactive binding
- `app._render_queue()` calls become `queue_display.items = list(queue.items)`
- The queue pause badge and item count are computed reactively
- Edit/remove actions stay on the widget (no app callback needed)
- `HarnessApp._queue` stays, but rendering moves from `QueuePanel` (SidePanel) to `QueueDisplay`

### What Moves Out of HarnessApp

- `_render_queue()` body moves into `QueueDisplay.refresh_items()`
- `QueuePanel.show_queue()` is replaced by the reactive `watch_items()` watcher
- Queue-related compose/mount logic moves to the widget

---

## Section 4: StreamRenderer Reactive Migration

### Current State

`StreamRenderer` has 17 instance variables, many of which are manually set and trigger manual `refresh()` calls. The `dirty_streams` set is manually drained each tick.

### Proposed Design

Add reactive annotations to key state, reducing manual refresh.

```
StreamRenderer (existing, not extracted)
├── reactive[int] live_run_tokens   # drives StatusBar.token_count
├── reactive[float] last_ttft       # drives StatusBar.ttft
├── reactive[str] current_model     # drives StatusBar.model_name
├── reactive[bool] rebuilding       # gates rendering during session replay
├── _dirty_streams: set             # kept as-is (performance: O(Δ) per tick)
└── watch_rebuilding()              # hides/shows transcript during replay
```

### Key Insight

`StreamRenderer` stays as a coordinator, not a Textual widget. Its `dirty_streams` set is a *performance optimization* — converting it to reactive would add overhead per-delta. Instead, we keep the explicit dirty-set for streaming deltas and add reactive only for *display-state* values (`live_run_tokens`, `last_ttft`, `current_model`) that are read by other widgets.

### What Changes

- `live_run_tokens` and `last_ttft` become reactive — StatusBar watches them directly
- The manual `refresh_status()` calls from StreamRenderer are deleted
- `rebuilding` becomes reactive — SubAgentsScreen and status bar can watch it
- The `subagents` list, `tool_widgets`, `dirty_streams` stay as plain attrs (internal to the renderer)

---

## Section 5: SubAgentsScreen → Screen Subclass

### Current State

`SubAgentsScreen` is a plain Python object (line 25 of `subagents/screen.py`) that uses `query_one` / `DataTable` directly. It's not in the Textual DOM, which limits composability and testability. It has `self.open` (bool), `self.index` (int), `self.dirty` (bool) with manual set/drain.

### Proposed Design

Convert to a proper `Screen` subclass.

```
SubAgentsScreen(Screen)  # was: plain class
├── reactive[bool] open      # drives display toggle
├── reactive[int] index      # selected row, drives detail pane
├── reactive[bool] dirty     # coalesced repaint trigger
├── compose() → [DataTable, TranscriptPane]
├── watch_open()             # mount/unmount or display toggle
├── watch_index()            # update detail pane selection
├── watch_dirty()            # trigger repaint
└── push_screen pattern      # pushed onto app.screen stack when activated
```

### Key Changes

- Convert from `SubAgentsScreen(self.app)` (plain object) to `self.push_screen(SubAgentsScreen())` (proper screen push)
- `self.open` reactive replaces the manual `display` toggling
- `self.dirty` reactive replaces the manual `dirty` → `drain_repaint()` pattern
- `self.index` reactive drives the detail pane selection
- The existing card/pane/screen files stay mostly unchanged — only `screen.py` gets the major refactor
- `app.subagents` reference changes from "plain object with methods" to "a screen that may or may not be on the stack"
- Add a guard: `if self.is_screen_installed(SubAgentsScreen): self.query_one(SubAgentsScreen).dirty = True`

---

## Section 6: HarnessApp Final Cleanup

After Sections 1-5, HarnessApp shrinks by ~200-300 lines (extracted state) and gains reactive patterns for the remainder.

### State That Stays on HarnessApp (Not Extractable)

- `self._turn_worker` — async Worker reference (can't be reactive)
- `self._turn_starting` — race latch (must be a plain bool)
- `self._quit_warned_at` — transient float (no render impact)
- `self._wake` — WakeDriver collaborator (not state)
- `self._session_view` — reference to SessionView (stays)
- `self._stream` — StreamRenderer reference (stays)
- `self._lsp` — LspManager reference (stays)

### State That Moves to Widgets

- `_status_busy`, `_model_name`, `_mode`, `_live_run_tokens`, `_last_ttft` → StatusBar
- `_compacting_notice`, compact lifecycle → CompactNotice
- `_refresh_queue()`, queue rendering → QueueDisplay
- Queue pause state → QueueDisplay

### What Remains on HarnessApp After Cleanup

- `compose()` (slimmed — widgets self-compose)
- Turn lifecycle (`_start_turn`, `_run_turn`, `_after_turn`) — core logic
- Command dispatch (`_route_submission`, bang handling)
- Session/compaction orchestration — calls `compact_notice.compacting = True`
- Mode change handler — calls `status_bar.mode = mode`

---

## Section 7: Testing Strategy

### Per-Widget Incremental Approach

For each widget extracted:
1. **Before extraction:** Write Pilot tests that verify the *current* behavior (e.g., "when compact starts, notice appears; when compact ends, notice disappears")
2. **During extraction:** Migrate to reactive, run the same tests — they should pass unchanged
3. **After extraction:** Add reactive-specific tests:
   - Watcher correctness: setting `compacting = True` shows the notice, `False` hides it
   - State propagation: `StatusBar.busy = True` → spinner appears
   - Race conditions: rapid `compacting = True/False` toggles don't strand widgets

### Textual Version Compatibility

The app's `async_teardown` in conftest.py already uses `await pilot.pause()` patterns. These work with reactive since reactive updates happen synchronously within the same tick.

### Coverage Target

The existing TUI tests run in CI with `--cov-fail-under=90`. The reactive migration should maintain or improve this. The new reactive watchers are pure state transitions, so they're fast to test.

---

## Migration Order

| Step | Widget | Difficulty | Lines Saved (approx) |
|------|--------|------------|---------------------|
| 1 | StatusBar | Easy | ~80 from HarnessApp |
| 2 | CompactNotice | Easy | ~40 from HarnessApp |
| 3 | QueueDisplay | Medium | ~60 from HarnessApp |
| 4 | StreamRenderer (reactive) | Medium | ~20 from HarnessApp |
| 5 | SubAgentsScreen → Screen | Hard | ~30 from HarnessApp |
| 6 | HarnessApp cleanup | Easy | ~70 (dead code, simplification) |

**Total estimated reduction:** ~300 lines from HarnessApp (1271 → ~970)

---

## Risks and Mitigations

| Risk | Mitigation |
|------|------------|
| Textual reactive perf on high-frequency updates (streaming) | Keep `dirty_streams` as a manual set; only add reactive for display-state values |
| SubAgentsScreen conversion breaks the overlay pattern | Use `push_screen` with `exclusive=False` to stack on top; test with existing Pilot tests |
| Reactive watchers fire out of order | Textual guarantees watcher execution order within a tick; no inter-watcher dependencies |
| Breaking the `_turn_starting` race latch | This stays as a plain bool — reactive can't replace async worker guards |

---

## Out of Scope

- Migrating `settings.py` to reactive (it already uses one reactive; no urgent need)
- Migrating `commands.py` (pure dispatch, no mutable state)
- Migrating widget-internal state in `ToolCallWidget`, `AssistantMessage`, etc. (they're already well-encapsulated)
- Changing Textual version constraints
