# Decompose HarnessApp Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract three cohesive clusters out of the `HarnessApp` god object into stateful collaborator objects (StatusPresenter, StreamRenderer, SessionView), each in its own file, leaving the App a thin composition-root + Textual-lifecycle shell — with **no behavior change**.

**Architecture:** Each collaborator is a plain object the App constructs in `__init__` (`self.status`, `self.stream`, `self.session`), holding `self.app` for DOM access and reaching siblings via `app.status` / `app.stream` / `app.session`. Moved methods keep their logic verbatim; the App keeps Textual-required surfaces (`compose`, `on_mount`, `BINDINGS`, `action_*`, message handlers) whose bodies become one-line delegations.

**Tech Stack:** Python 3.10+, Textual 8.2.7, pytest + anyio, ruff (line-length 100), pyright.

## Global Constraints

- **No behavior change.** The TUI renders and acts identically. `uv run pytest` stays green at every step.
- **Incremental:** one collaborator per task; full suite green between tasks; never bundle the three into one commit.
- Collaborators take the `HarnessApp` as `app` and use `app.query_one(...)`, `app.mount(...)`, `app.title`, `app._driver`, `app.harness`, and `app.status`/`app.stream`/`app.session` for siblings.
- The App retains these names as **public delegations** (other code/tests call them): `start_new_session`, `switch_to_session_id`, `reset_conversation`, `post_system`, `action_cycle_mode`, `action_toggle_outputs`, `action_cancel_turn`.
- Method-move transform (apply verbatim when moving a method body from `HarnessApp` to a collaborator):
  - collaborator-owned state `self._X` → `self._X` (now an attribute of the collaborator);
  - App-owned things → `self.app.X` (e.g. `self.app.harness`, `self.app.query_one`, `self.app.deps`);
  - sibling collaborator calls → `self.app.status.*` / `self.app.stream.*` / `self.app.session.*`.
- ruff line-length 100; pyright must stay green.
- Tests live flat in `tests/`. Run targeted files per task plus the full suite before commit.

---

### Task 1: Extract `StatusPresenter`

Move the status-bar / title / spinner / session+turn-timer cluster into a new collaborator. Most self-contained — do it first.

**Files:**
- Create: `src/marim_harness/interfaces/tui/status.py`
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `tests/test_app.py` (reach-in updates)
- Create: `tests/test_app_decomposition.py`

**Interfaces:**
- Produces: `StatusPresenter(app)` with attributes `busy: bool`, `spin: int`, `session_start: float`, `turn_start: float`; methods `status_text() -> Content`, `refresh_status() -> None`, `refresh_title() -> None`, `tick_spinner() -> None`, `set_busy(busy: bool) -> None`. Module-level `format_duration(seconds, *, precise=False)` and `osc_title(text)` live here.
- Consumes (Task 2 will satisfy; until then read off the App): the in-flight token tally. For Task 1, `status_text` reads `self.app._live_run_tokens` and `set_busy` resets `self.app._live_run_tokens = 0` (those move to `StreamRenderer` in Task 2; this line is updated then).

- [ ] **Step 1: Create `status.py` with `StatusPresenter`**

```python
# src/marim_harness/interfaces/tui/status.py
"""The status bar, terminal title, working-spinner, and session/turn timers —
extracted from HarnessApp. Holds its own busy/spin/timer state; reaches the app
and sibling collaborators through `self.app`."""

import time
from typing import TYPE_CHECKING

from textual.content import Content
from textual.css.query import NoMatches
from textual.widgets import Static

from ...compaction import estimate_tokens
from ...usage import resolve_cost
from .widgets import format_cost, format_token_split, human_tokens

if TYPE_CHECKING:
    from .app import HarnessApp

_CLOCK_TICK_INTERVAL = 1.0
_SPINNER = "⠋⠙⠹⠸⠼⠴⠦⠧⠇⠏"
_SPINNER_TICK_INTERVAL = 0.1


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


def format_duration(seconds: float, *, precise: bool = False) -> str:
    if seconds < 60:
        return f"{seconds:.1f}s" if precise else f"{int(seconds)}s"
    minutes = int(seconds // 60)
    if minutes < 60:
        return f"{minutes}m"
    return f"{minutes // 60}h {minutes % 60}m"


class StatusPresenter:
    def __init__(self, app: "HarnessApp") -> None:
        self.app = app
        self.busy = False
        self.spin = 0
        self.session_start = time.monotonic()
        self.turn_start = time.monotonic()

    # status_text / refresh_status / refresh_title / tick_spinner / set_busy:
    # move the bodies of HarnessApp._status_text/_refresh_status/_refresh_title/
    # _tick_spinner/_set_busy here verbatim, applying the transform:
    #   self._busy/_spin/_session_start/_turn_start -> self._<same> on this object
    #   self.harness -> self.app.harness ; self.query_one -> self.app.query_one
    #   self.title = X -> self.app.title = X ; self._driver -> self.app._driver
    #   self._live_run_tokens -> self.app._live_run_tokens  (until Task 2)
    #   _format_duration -> format_duration ; _osc_title -> osc_title
    #   _SPINNER/_CLOCK_TICK_INTERVAL/_SPINNER_TICK_INTERVAL -> module-local here
```

Move the five method bodies in full (they are ~10–25 lines each) under the
transform noted in the class. `set_busy(busy)` keeps its exact logic: set
`self.busy`; if busy `self.spin = 0` else `self.app._live_run_tokens = 0`; then
`self.refresh_title()` and `self.refresh_status()`.

- [ ] **Step 2: Wire it into `app.py` and delegate**

In `HarnessApp.__init__`, after `self.harness = ...`, add:
```python
from .status import StatusPresenter
self.status = StatusPresenter(self)
```
Delete the moved `_status_text`/`_refresh_status`/`_refresh_title`/`_tick_spinner`/`_set_busy` methods, the module-level `_format_duration`/`_osc_title`, the `_CLOCK_TICK_INTERVAL`/`_SPINNER`/`_SPINNER_TICK_INTERVAL` constants, and the `_busy`/`_spin`/`_session_start`/`_turn_start` `__init__` assignments, from `app.py`. Replace every in-file use:
- `self._busy` → `self.status.busy` (in `_run_turn`, `action_cancel_turn`, `_flush_streams`)
- `self._refresh_status()` → `self.status.refresh_status()` (all call sites)
- `self._refresh_title()` → `self.status.refresh_title()`
- `self._set_busy(x)` → `self.status.set_busy(x)`
- `self._tick_spinner` → `self.status.tick_spinner`
- `self._turn_start` (in `_run_turn` stamp) → `self.status.turn_start`
- `self._session_start = time.monotonic()` in `on_mount` → `self.status.session_start = time.monotonic()`
- the two `set_interval(...)` lines → `self.set_interval(_STREAM_FLUSH_INTERVAL, self._flush_streams)` stays; the clock+spinner intervals become `self.set_interval(status._CLOCK_TICK_INTERVAL, self.status.refresh_status)` and `self.set_interval(status._SPINNER_TICK_INTERVAL, self.status.tick_spinner)` — import the intervals from `.status` (e.g. `from .status import _CLOCK_TICK_INTERVAL, _SPINNER_TICK_INTERVAL`).
- `_format_duration(...)` used in `_run_turn` for the TurnMeta stamp → import `format_duration` from `.status` and call `format_duration(time.monotonic() - self.status.turn_start, precise=True)`.

- [ ] **Step 3: Update the reach-in tests in `tests/test_app.py`**

Mechanical relocation (an internal handle moved; no behavior change). Replace:
- `app._busy` → `app.status.busy`
- `app._spin` → `app.status.spin`
- `app._set_busy(` → `app.status.set_busy(`
- `app._refresh_status(` → `app.status.refresh_status(`
- `app._refresh_title(` → `app.status.refresh_title(`
- `app._tick_spinner(` → `app.status.tick_spinner(`
- `app._session_start` → `app.status.session_start`
- `app._turn_start` → `app.status.turn_start`
- (leave `app._live_run_tokens` and `app._show_all_output` as-is — they move in Task 2)

Run `grep -nE "app\._(busy|spin|set_busy|refresh_status|refresh_title|tick_spinner|session_start|turn_start)\b" tests/` and confirm zero remain.

- [ ] **Step 4: Add the decomposition test**

```python
# tests/test_app_decomposition.py
from pathlib import Path

import pytest

from marim_harness.interfaces.tui.status import StatusPresenter


def _app(tmp_path):
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    from marim_harness.interfaces.tui.app import HarnessApp

    return HarnessApp(Harness(TestModel(call_tools=[]), BuiltinToolProvider(),
                              deps, instructions="test"))


@pytest.mark.anyio
async def test_status_presenter_owns_busy_and_drives_title(tmp_path: Path):
    app = _app(tmp_path)
    assert isinstance(app.status, StatusPresenter)
    async with app.run_test() as pilot:
        await pilot.pause()
        app.status.set_busy(True)
        assert app.status.busy is True
        assert not hasattr(app, "_busy")  # state truly moved, no shim left behind
```

- [ ] **Step 5: Run tests, lint, types**

```bash
uv run pytest tests/test_app.py tests/test_app_decomposition.py tests/test_widgets.py -q
uv run ruff check src/marim_harness/interfaces/tui/status.py src/marim_harness/interfaces/tui/app.py tests/test_app.py tests/test_app_decomposition.py
uv run pyright src/marim_harness/interfaces/tui/status.py src/marim_harness/interfaces/tui/app.py
uv run pytest    # full suite green
```
Expected: all pass; ruff clean; pyright 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/status.py src/marim_harness/interfaces/tui/app.py tests/test_app.py tests/test_app_decomposition.py
git commit -m "refactor(tui): extract StatusPresenter from HarnessApp"
```

---

### Task 2: Extract `StreamRenderer`

Move the event→widget streaming engine (and the `_StreamSink` classes) into a collaborator.

**Files:**
- Create: `src/marim_harness/interfaces/tui/stream_render.py`
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `src/marim_harness/interfaces/tui/status.py` (one line: read tokens off the renderer)
- Modify: `tests/test_app.py`, `tests/test_commands.py` (reach-in updates)

**Interfaces:**
- Consumes: `app.status.refresh_status()` (from Task 1).
- Produces: `StreamRenderer(app)` with attributes `current_assistant`, `tool_widgets`, `tool_group`, `solo_tool`, `sub_tool_groups`, `sub_solo_tools`, `sub_assistants`, `dirty_streams`, `live_run_tokens: int`, `show_all_output: bool`; methods `on_events(ctx, events)`, `on_subagent_event(event)`, `dispatch_stream_event(sink, event)`, `add_tool_to_run(...)`, `mount_spawn_widget(args)`, `append_stream(widget, delta)`, `flush_streams()`, `toggle_reveal_all()`, `reset()`, `reset_live_tokens()`.

- [ ] **Step 1: Create `stream_render.py`**

Move the `_StreamSink`, `_TopLevelSink`, `_SubAgentSink` classes and the streaming
methods (`_on_events`, `_on_subagent_event`, `_dispatch_stream_event`,
`_add_tool_to_run`, `_mount_spawn_widget`, `_append_stream`, `_flush_streams`) into
a `StreamRenderer` class, applying the move transform. The sink classes currently
reference `self._app._current_assistant` etc.; those become `self._app.stream.*`
(or, since the sinks already hold the app, give them the renderer:
`_TopLevelSink(renderer, container)` reading `renderer.current_assistant`). Add:
```python
def reset(self) -> None:
    """Clear per-session stream state when the log is rebuilt."""
    self.current_assistant = None
    self.tool_widgets.clear()
    self.tool_group = None
    self.solo_tool = None
    self.sub_tool_groups.clear()
    self.sub_solo_tools.clear()
    self.sub_assistants.clear()
    self.dirty_streams.clear()

def reset_live_tokens(self) -> None:
    self.live_run_tokens = 0

def toggle_reveal_all(self) -> None:
    self.show_all_output = not self.show_all_output
    for group in self.app.query(ToolGroupWidget):
        group.collapsed = not self.show_all_output
    for widget in self.app.query(ToolCallWidget):
        widget.set_reveal(self.show_all_output)
```
`flush_streams` keeps its logic; its `if self._busy:` guard → `if self.app.status.busy:` and `self._refresh_status()` → `self.app.status.refresh_status()`.

- [ ] **Step 2: Wire into `app.py`**

In `__init__`: `from .stream_render import StreamRenderer; self.stream = StreamRenderer(self)`. Delete the moved methods, the three sink classes, and the moved `__init__` state. Update remaining App code:
- `_run_turn` passes `event_stream_handler=self.stream.on_events` (and the hooked handler wraps `self.stream.on_events`).
- the `on_subagent_event` deps callback → `self.stream.on_subagent_event`.
- the flush interval → `self.set_interval(_STREAM_FLUSH_INTERVAL, self.stream.flush_streams)`.
- `action_toggle_outputs` body → `self.stream.toggle_reveal_all()`; delete `self._show_all_output` from the App (now `self.stream.show_all_output`).
- any `self._current_assistant` (e.g. in `_run_turn` for the TurnMeta stamp / clearing) → `self.stream.current_assistant`.

- [ ] **Step 3: Update `status.py`**

`StatusPresenter.status_text`: `self.app._live_run_tokens` → `self.app.stream.live_run_tokens`. `StatusPresenter.set_busy`: the idle reset `self.app._live_run_tokens = 0` → `self.app.stream.reset_live_tokens()`.

- [ ] **Step 4: Update reach-in tests**

In `tests/test_app.py` and `tests/test_commands.py`:
- `app._on_events` → `app.stream.on_events`
- `app._tool_widgets` → `app.stream.tool_widgets`
- `app._live_run_tokens` → `app.stream.live_run_tokens`
- `app._show_all_output` → `app.stream.show_all_output`
- `self._current_assistant` / `app._current_assistant` → `…stream.current_assistant`

Run `grep -nE "app\._(on_events|tool_widgets|live_run_tokens|show_all_output|current_assistant|flush_streams|append_stream)\b" tests/` and confirm zero remain.

- [ ] **Step 5: Tests, lint, types, commit**

```bash
uv run pytest tests/test_app.py tests/test_commands.py tests/test_widgets.py tests/test_app_decomposition.py -q
uv run ruff check src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/status.py tests/test_app.py tests/test_commands.py
uv run pyright src/marim_harness/interfaces/tui/stream_render.py src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/status.py
uv run pytest
git add -A && git commit -m "refactor(tui): extract StreamRenderer from HarnessApp"
```
Expected: all green, ruff clean, pyright 0 errors.

---

### Task 3: Extract `SessionView`

Move the session-rebuild orchestration into a collaborator.

**Files:**
- Create: `src/marim_harness/interfaces/tui/session_view.py`
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `tests/test_app.py` (reach-in updates, if any remain)

**Interfaces:**
- Consumes: `app.stream.reset()`, `app.status.refresh_status()` / `refresh_title()`, `app.post_system`, `app.harness` session APIs.
- Produces: `SessionView(app)` with methods `render_session(note)`, `reset_conversation()`, `start_new_session(name=None)`, `switch_to_session_id(id)`, `replay_history(log)`, `on_rename(old, new)`.

- [ ] **Step 1: Create `session_view.py`**

Move `_render_session`, `reset_conversation`, `start_new_session`,
`switch_to_session_id`, `_replay_history`, `_on_rename` bodies into `SessionView`
under the transform. `render_session` resets stream state via `self.app.stream.reset()`
(replacing the inline `self._current_assistant = None; self._tool_widgets.clear()`),
then rebuilds the log and calls `self.app.status.refresh_title()` /
`refresh_status()`. `on_rename` mounts the rename notice and calls
`self.app.status.refresh_title()` + `refresh_status()`.

- [ ] **Step 2: Wire into `app.py`**

`__init__`: `from .session_view import SessionView; self.session = SessionView(self)`. Wire the harness rename callback to `self.session.on_rename`. Keep thin public delegations on the App so external callers/tests are unaffected:
```python
async def reset_conversation(self) -> None:
    await self.session.reset_conversation()

async def start_new_session(self, name: str | None = None) -> None:
    await self.session.start_new_session(name)

async def switch_to_session_id(self, session_id: str) -> None:
    await self.session.switch_to_session_id(session_id)
```
Delete the moved method bodies from the App.

- [ ] **Step 3: Update any reach-in tests**

`grep -nE "app\._(render_session|replay_history|on_rename)\b" tests/` → update to `app.session.*` if present. (`start_new_session`/`switch_to_session_id`/`reset_conversation` keep working via the delegations — no test change.)

- [ ] **Step 4: Tests, lint, types, commit**

```bash
uv run pytest tests/test_app.py tests/test_commands.py tests/test_widgets.py tests/test_app_decomposition.py -q
uv run ruff check src/marim_harness/interfaces/tui/session_view.py src/marim_harness/interfaces/tui/app.py tests/test_app.py
uv run pyright src/marim_harness/interfaces/tui/session_view.py src/marim_harness/interfaces/tui/app.py
uv run pytest
git add -A && git commit -m "refactor(tui): extract SessionView from HarnessApp"
```
Expected: all green.

---

## Final verification

- [ ] **Confirm the App shrank and the suite is green**

```bash
uv run pytest
uv run ruff check src tests
uv run pyright
awk '/^class HarnessApp/{f=1} f&&/^    (async )?def /{c++} END{print c" methods on HarnessApp"}' src/marim_harness/interfaces/tui/app.py
wc -l src/marim_harness/interfaces/tui/app.py
```
Expected: all green; method count and line count materially lower than the pre-refactor 45 methods / 941 lines; the three collaborator files each hold one responsibility.
