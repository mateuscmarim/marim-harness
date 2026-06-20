# Decompose HarnessApp into stateful collaborators — design

**Date:** 2026-06-19
**Status:** Approved (design); implementation plan to follow.

## Goal

`HarnessApp` (interfaces/tui/app.py) has grown into a god object: ~725 lines, 45
methods, 36 instance attributes, juggling ~9 responsibilities. Extract three
cohesive clusters into stateful collaborator objects, each in its own file, so the
App becomes a thin composition-root + Textual-lifecycle shell. **Pure refactor —
no behavior change.**

## Constraints

- **No behavior change.** The TUI must render and act identically. The existing
  suite (tests/test_app.py, tests/test_widgets.py) is the safety net and must stay
  green at every step.
- **Incremental.** One collaborator extracted per step; full suite green between
  steps; never bundle the three into one commit.
- Each collaborator is a plain object constructed by the App, holding `self.app`
  (the `HarnessApp`) for DOM access (`query_one`, `mount`, `title`, `_driver`), and
  reaching sibling collaborators via `app.status` / `app.stream` / `app.session`.
- Keep public/Textual-required surfaces on the App: `compose`, `on_mount`,
  `on_unmount`, `BINDINGS`, `action_*`, reactive watchers (`watch_theme`), and
  message handlers (`on_prompt_input_submitted`). Their bodies delegate.

## Collaborators

### 1. `StatusPresenter` (interfaces/tui/status.py)
- **State (moves off App):** `busy`, `spin`, `session_start`, `turn_start`.
- **Methods (moved):** `status_text() -> Content`, `refresh_status()`,
  `refresh_title()`, `tick_spinner()`, `set_busy(busy: bool)`.
- **Module helpers moved here:** `_format_duration`, `_osc_title`, and the
  constants `_CLOCK_TICK_INTERVAL`, `_SPINNER`, `_SPINNER_TICK_INTERVAL`,
  `_DIFF_CAP` stays in widgets (it's a widget concern) — only the status-owned
  constants move.
- **Reads (via app):** `app.harness` (model label/id, session usage, session name),
  `app.stream.live_run_tokens` (the in-flight `+N` token display).
- **set_busy** resets the in-flight tally via `app.stream.reset_live_tokens()` when
  going idle, then refreshes title + status.
- App delegations: `_status_text`/`_refresh_status`/`_refresh_title`/`_tick_spinner`/
  `_set_busy` become `self.status.*`. The on_mount intervals call
  `self.status.refresh_status` and `self.status.tick_spinner`.

### 2. `StreamRenderer` (interfaces/tui/stream_render.py)
- **State (moves off App):** `current_assistant`, `tool_widgets`, `tool_group`,
  `solo_tool`, `sub_tool_groups`, `sub_solo_tools`, `sub_assistants`,
  `dirty_streams`, `live_run_tokens`, `show_all_output`.
- **Classes moved here:** `_StreamSink`, `_TopLevelSink`, `_SubAgentSink` (they read
  the stream state, which now lives on the renderer, not the app).
- **Methods (moved):** `on_events`, `on_subagent_event`, `dispatch_stream_event`,
  `add_tool_to_run`, `mount_spawn_widget`, `append_stream`, `flush_streams`,
  `toggle_reveal_all()` (the Ctrl+O body), `reset()` (clear per-session stream
  state, used by SessionView), `reset_live_tokens()`.
- **Calls (via app):** `app.status.refresh_status()` while flushing during a turn.
- App delegations: `_on_events`/`_on_subagent_event` (the `event_stream_handler`
  passed to `harness.run_turn`) call `self.stream.*`; `_flush_streams` interval →
  `self.stream.flush_streams`; `action_toggle_outputs` → `self.stream.toggle_reveal_all()`
  (keeps `_show_all_output` on the renderer).

### 3. `SessionView` (interfaces/tui/session_view.py)
- **State:** minimal/none (behavior orchestrator).
- **Methods (moved):** `render_session(note)`, `reset_conversation()`,
  `start_new_session(name)`, `switch_to_session_id(id)`, `replay_history(log)`,
  `on_rename(old, new)`.
- **Calls (via app):** `app.stream.reset()` (clear stream state on rebuild),
  `app.status.refresh_status()` / `app.status.refresh_title()`, `app.post_system`,
  the harness session APIs.
- App delegations: the public methods (`reset_conversation`, `start_new_session`,
  `switch_to_session_id`) and the `on_rename` harness callback delegate to
  `self.session.*`.

## What stays on `HarnessApp`

Construction/wiring of the three collaborators (in `__init__`); Textual lifecycle
(`compose`, `on_mount`, `on_unmount`, `_connect_mcp`, intervals); `BINDINGS` and the
`action_*` methods (thin delegations); `watch_theme`; message/turn handling
(`on_prompt_input_submitted`, `_run_turn` — which times the turn via
`app.status` and stamps the `TurnMeta`); the autonomous-wake machinery
(`autonomous_wake`, `_wake_depth_cap`, `_auto_turn_depth`, `_maybe_wake`); vision
gating (`_vision_caps`, `_refresh_vision_caps`, `_image_block_reason`,
`_on_model_chosen`, `open_model_picker`); compaction feedback
(`_on_compact_start`, `_on_compact`, `_compacting_notice`); the task/job panels
(`_render_tasks`, `_on_tasks_changed`, `_render_jobs`, `_on_jobs_changed`); modals
(`open_settings`, `_request_approval`, `_ask_user`); `post_system`; `_notify`.

## Wiring order in `__init__`

`self.status`, `self.stream`, `self.session` are constructed after `self.harness`
is set. Because they only reach each other lazily through `self.app.<sibling>`
(method-call time, not construction time), construction order among the three does
not matter.

## Migration order (each a green-suite checkpoint, separate commit)

1. **StatusPresenter** — most self-contained. Move state + 5 methods + the two
   module helpers; App delegates. (The stream methods still on the App call
   `self.status.refresh_status()`.)
2. **StreamRenderer** — move stream state + the 3 sink classes + methods; App's
   `event_stream_handler` and the flush interval delegate; `StatusPresenter` now
   reads `app.stream.live_run_tokens`.
3. **SessionView** — move the session-rebuild methods; App delegates; it calls
   `app.stream.reset()` + `app.status` refreshes.

## Testing

- The existing tests/test_app.py + tests/test_widgets.py suites must pass unchanged
  after each step (the behavior-preservation guarantee). Where an existing test
  reaches into a moved attribute/method directly (e.g. `app._busy`,
  `app._current_assistant`, `app._refresh_title`, `app.action_toggle_outputs`),
  update the test to the new location (`app.status.busy`,
  `app.stream.current_assistant`, `app.status.refresh_title`, …) — these are
  mechanical relocations of an internal handle, not behavior changes.
- Add a small `tests/test_app_decomposition.py`: after mount, `app.status`,
  `app.stream`, `app.session` exist and are the right types; a representative
  delegation works end-to-end (e.g. `app.action_toggle_outputs()` toggles
  `app.stream.show_all_output`; `app._set_busy`-equivalent flips `app.status.busy`).
- Keep public delegations: any code/tests calling `app.start_new_session(...)`,
  `app.switch_to_session_id(...)`, `app.reset_conversation()` keep working (the App
  retains those names, delegating).

## Risks & mitigations

- **Central file, high blast radius** → incremental, suite-green gates, no bundled
  commit.
- **Hidden attribute reach-ins** from tests/other modules → grep for `app._busy`,
  `_current_assistant`, `_refresh_*`, `_flush_streams`, `_on_events`,
  `_render_session`, etc. before each step and update call sites in the same commit.
- **Textual context** (collaborators need a live app for `query_one`/`mount`) →
  collaborators take the App and use `app.query_one`/`app.mount`; pure logic stays
  unit-testable, DOM-touching paths covered by the mounted suite.

## Build order (plan)

1. Extract `StatusPresenter` (+ update reach-in call sites, + decomposition test stub).
2. Extract `StreamRenderer`.
3. Extract `SessionView`.
