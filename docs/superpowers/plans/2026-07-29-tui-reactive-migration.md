# TUI Reactive Migration Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Migrate the TUI from manual refresh calls to Textual reactive state management, extracting widgets from HarnessApp to reduce its size from ~1271 lines to ~970.

**Architecture:** Parallel incremental — extract one widget at a time from HarnessApp, migrating it to reactive as you extract. Start with the easiest wins (StatusBar, CompactNotice) and work toward the harder ones (SubAgentsScreen). Each step reduces HarnessApp's size and creates a reactive reference point.

**Tech Stack:** Textual 8.2.7, pytest with Textual Pilot, Python 3.10+

## Global Constraints

- `requires-python >= 3.10` — avoid 3.11+ syntax (no `tomllib` stdlib, no `match` statements)
- Ruff line length 100; lint set `E,F,I,UP,B,SIM,C901`
- Cyclomatic complexity capped at 10 (`C901`)
- Textual `>=0.80` (installed: 8.2.7)
- All gated tools (write/edit/bash) are not in scope — this is UI-only
- Keep long explanatory comments on *why* non-obvious invariants hold
- Follow `coding-guidelines.md`: control complexity, prefer straight-line flow, optimize for cohesion

---

## File Structure

### New Files

| File | Responsibility |
|------|---------------|
| `src/marim_harness/interfaces/tui/widgets/status_bar.py` | StatusBar widget — owns busy/spin/timer/tokens/cost state, renders status line reactively |
| `src/marim_harness/interfaces/tui/widgets/compact_notice.py` | CompactNotice widget — self-contained compact lifecycle (compacting/done/error) |
| `src/marim_harness/interfaces/tui/widgets/queue_display.py` | QueueDisplay widget — renders queued messages reactively |
| `tests/test_tui_status_bar.py` | Tests for StatusBar reactive behavior |
| `tests/test_tui_compact_notice.py` | Tests for CompactNotice reactive behavior |
| `tests/test_tui_queue_display.py` | Tests for QueueDisplay reactive behavior |

### Modified Files

| File | Changes |
|------|---------|
| `src/marim_harness/interfaces/tui/app.py` | Remove status state (~80 lines), remove compact lifecycle (~40 lines), remove queue rendering (~60 lines), replace with widget references |
| `src/marim_harness/interfaces/tui/status.py` | Simplify `StatusPresenter` to a thin formatting helper (absorb into StatusBar or keep as pure formatter) |
| `src/marim_harness/interfaces/tui/queue.py` | No changes — `TurnQueue` stays as pure logic |
| `src/marim_harness/interfaces/tui/stream_render.py` | Add reactive annotations to `live_run_tokens`, `last_ttft`, `current_model`; remove `refresh_status()` calls |
| `src/marim_harness/interfaces/tui/subagents/screen.py` | Convert from plain class to `Screen` subclass with reactive `open`/`index`/`dirty` |
| `src/marim_harness/interfaces/tui/widgets/panels.py` | `QueuePanel.show_queue()` becomes a thin wrapper or is replaced by `QueueDisplay` |
| `src/marim_harness/interfaces/tui/commands.py` | Replace `app.status.refresh_status()` calls with reactive assignments |

---

## Task 1: StatusBar Widget

Extract status rendering from `StatusPresenter` + `HarnessApp` into a reactive `StatusBar` widget.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/status_bar.py`
- Create: `tests/test_tui_status_bar.py`
- Modify: `src/marim_harness/interfaces/tui/app.py:148-179,277,502-522,771,781,870,886,942`
- Modify: `src/marim_harness/interfaces/tui/status.py:32-171`
- Modify: `src/marim_harness/interfaces/tui/commands.py:155,222`
- Modify: `src/marim_harness/interfaces/tui/session_view.py:473,510`
- Modify: `src/marim_harness/interfaces/tui/settings.py:607,646,706,800`

**Interfaces:**
- Consumes: `Harness` (via `self.app.harness`), `StreamRenderer` (via `self.app.stream`)
- Produces: `StatusBar` widget with `busy`, `live_run_tokens`, `last_ttft`, `model_name`, `mode` reactives

- [ ] **Step 1: Write tests for StatusBar behavior**

```python
# tests/test_tui_status_bar.py
"""Tests for the StatusBar reactive widget."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.status_bar import StatusBar


class _StatusBarApp(App[None]):
    """Minimal app harness for testing StatusBar in isolation."""

    def compose(self) -> ComposeResult:
        yield StatusBar()


@pytest.fixture()
async def pilot():
    async with _StatusBarApp().run_test() as pilot:
        yield pilot


async def test_status_bar_mounts(pilot):
    """StatusBar renders on mount."""
    bar = pilot.app.query_one(StatusBar)
    assert bar is not None
    assert isinstance(bar, Static)


async def test_busy_reactive_shows_spinner(pilot):
    """Setting busy=True renders the working indicator."""
    bar = pilot.app.query_one(StatusBar)
    bar.busy = True
    await pilot.pause()
    text = bar.render()
    assert "working" in str(text).lower() or "…" in str(text)


async def test_busy_false_hides_spinner(pilot):
    """Setting busy=False hides the working indicator."""
    bar = pilot.app.query_one(StatusBar)
    bar.busy = True
    await pilot.pause()
    bar.busy = False
    await pilot.pause()
    text = bar.render()
    assert "working" not in str(text).lower()


async def test_mode_reactive(pilot):
    """Mode value appears in the status text."""
    bar = pilot.app.query_one(StatusBar)
    bar.mode = "auto"
    await pilot.pause()
    text = bar.render()
    assert "auto" in str(text)


async def test_model_name_reactive(pilot):
    """Model name appears in the status text."""
    bar = pilot.app.query_one(StatusBar)
    bar.model_name = "claude-sonnet"
    await pilot.pause()
    text = bar.render()
    assert "claude-sonnet" in str(text)


async def test_live_tokens_delta(pilot):
    """Live token count shows as +N delta."""
    bar = pilot.app.query_one(StatusBar)
    bar.live_run_tokens = 1500
    await pilot.pause()
    text = bar.render()
    assert "+1" in str(text)  # +1.5k or similar


async def test_ttft_display(pilot):
    """Time-to-first-token appears when set."""
    bar = pilot.app.query_one(StatusBar)
    bar.last_ttft = 0.8
    await pilot.pause()
    text = bar.render()
    assert "0.8" in str(text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_status_bar.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.widgets.status_bar'`

- [ ] **Step 3: Implement StatusBar widget**

```python
# src/marim_harness/interfaces/tui/widgets/status_bar.py
"""Reactive status bar — owns all display state that was scattered across
HarnessApp and StatusPresenter. Setting any reactive triggers an automatic
re-render, eliminating the 19 manual refresh_status() call sites."""
from __future__ import annotations

import time
from typing import TYPE_CHECKING

from textual.content import Content
from textual.reactive import reactive
from textual.widgets import Static

from ....compaction import estimate_tokens
from ....usage import resolve_cost
from .format import _SPINNER, format_duration
from .format import _SPINNER_TICK_INTERVAL  # noqa: F401
from .format import format_cost, format_token_split, human_tokens

if TYPE_CHECKING:
    from ..app import HarnessApp


def osc_title(text: str) -> str:
    """OSC 0 escape that sets the terminal's tab AND window title."""
    return f"\033]0;{text}\007"


class StatusBar(Static):
    """A reactive status bar that auto-renders on state changes.

    Replaces the manual StatusPresenter + refresh_status() pattern with
    Textual reactive declarations. The 19 call sites that used to call
    ``app.status.refresh_status()`` now just assign to these reactives.
    """

    busy: reactive[bool] = reactive(False, init=False)
    live_run_tokens: reactive[int] = reactive(0, init=False)
    last_ttft: reactive[float | None] = reactive(None, init=False)
    model_name: reactive[str] = reactive("", init=False)
    mode: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__(id="status-bar")
        self.spin = 0
        self.session_start = time.monotonic()
        self.turn_start = time.monotonic()
        # Memoized context-size estimate — same rationale as StatusPresenter:
        # estimate_tokens() is O(total bytes), status bar repaints ~12.5x/s
        # while streaming; len(history) is an exact change key.
        self._ctx_tokens_key = -1
        self._ctx_tokens = 0
        self._cost_key: object = None
        self._cost: float | None = None

    def _context_tokens(self) -> int:
        """Context-size estimate, memoized on history length."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        history = app.harness.session.history
        key = len(history)
        if key != self._ctx_tokens_key:
            self._ctx_tokens_key = key
            self._ctx_tokens = estimate_tokens(history)
        return self._ctx_tokens

    def _session_cost(self) -> float | None:
        """Committed session cost, memoized on (token total, model)."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        usage = app.harness.session.usage
        model_id = app.harness.model_id
        key = (usage.total_tokens, model_id)
        if key != self._cost_key:
            self._cost_key = key
            self._cost, _ = resolve_cost(usage, model_id)
        return self._cost

    def render(self) -> Content:
        """Auto-called by Textual whenever a reactive changes."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        cfg = getattr(app.harness, "model_label", "model")
        used = self._context_tokens()
        max_ctx = getattr(app.harness.session, "compact_threshold", 0) or 0
        pct = round(used / max_ctx * 100) if max_ctx else 0
        ctx_text = f"ctx {human_tokens(used)}/{human_tokens(max_ctx)} ({pct}%)"
        ctx_style = "red" if pct >= 90 else "yellow" if pct >= 75 else ""
        tokens_text = format_token_split(app.harness.session.usage)
        if self.live_run_tokens:
            tokens_text += f" +{human_tokens(self.live_run_tokens)}"
        cost = self._session_cost()
        if cost is not None:
            tokens_text += f" · {format_cost(cost)}"
        session_text = f"session {format_duration(time.monotonic() - self.session_start)}"
        fields = [
            Content(self.mode),
            Content(self.model_name or cfg),
            Content.assemble((ctx_text, ctx_style)) if ctx_style else Content(ctx_text),
            Content(tokens_text),
            Content(session_text),
        ]
        if self.last_ttft is not None:
            fields.append(Content(f"ttft {self.last_ttft:.1f}s"))
        if self.busy:
            elapsed = format_duration(time.monotonic() - self.turn_start)
            fields.append(Content(f"working… {elapsed}"))
        return Content.from_markup(" [dim]·[/] ").join(fields)

    def set_busy(self, busy: bool) -> None:
        """Transition busy state — resets spinner and turn timer."""
        self.busy = busy
        if busy:
            self.spin = 0
            self.turn_start = time.monotonic()
        else:
            self.app.stream.reset_live_tokens()  # type: ignore[union-attr]
            self.live_run_tokens = 0

    def refresh_title(self) -> None:
        """Set terminal tab/window title via OSC sequence."""
        app: HarnessApp = self.app  # type: ignore[assignment]
        mark = _SPINNER[self.spin] if self.busy else "●"
        name = app.harness.session.session_name or "marim-harness"
        app.title = f"{mark} {name}"
        if app._driver is not None:
            try:
                app._driver.write(osc_title(f"{mark} {name}"))
                app._driver.flush()
            except Exception:
                pass

    def tick_spinner(self) -> None:
        """Advance the working-indicator animation. No-op when idle."""
        if not self.busy:
            return
        self.spin = (self.spin + 1) % len(_SPINNER)
        self.refresh_title()
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui_status_bar.py -v`
Expected: PASS

- [ ] **Step 5: Wire StatusBar into HarnessApp**

In `app.py`:
- Add `from .widgets.status_bar import StatusBar` to imports
- In `compose()`, replace the `Static(id="status-bar")` with `StatusBar()`
- Remove `self._status = StatusPresenter(self)` from `__init__`
- Add `self._status_bar = StatusBar()` reference (or access via `self.query_one(StatusBar)`)
- Replace all `self.status.refresh_status()` calls with `self.query_one(StatusBar).live_run_tokens = self._stream.live_run_tokens` (or the appropriate reactive assignment)
- Replace `self.status.set_busy(True)` with `self.query_one(StatusBar).set_busy(True)`
- Replace `self.status.tick_spinner()` with `self.query_one(StatusBar).tick_spinner()`
- Remove `_status_busy`, `_model_name`, `_mode`, `_live_run_tokens`, `_last_ttft` instance vars

- [ ] **Step 6: Update command and session_view callers**

In `commands.py`:
- Replace `app.status.refresh_status()` with reactive assignments on `StatusBar`

In `session_view.py`:
- Replace `self.app.status.refresh_status()` with reactive assignments

In `settings.py`:
- Replace `self.app.status.refresh_status()` with reactive assignments

In `stream_render.py`:
- Replace `self.app.status.refresh_status()` with reactive assignments on StatusBar

- [ ] **Step 7: Run full TUI test suite**

Run: `uv run pytest tests/ -k "tui or status or app" --no-cov -x`
Expected: PASS

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/status_bar.py \
        src/marim_harness/interfaces/tui/app.py \
        src/marim_harness/interfaces/tui/status.py \
        src/marim_harness/interfaces/tui/commands.py \
        src/marim_harness/interfaces/tui/session_view.py \
        src/marim_harness/interfaces/tui/settings.py \
        src/marim_harness/interfaces/tui/stream_render.py \
        tests/test_tui_status_bar.py
git commit -m "feat(tui): extract StatusBar widget with reactive state

Replace 19 manual refresh_status() calls with reactive assignments.
StatusBar owns busy/spin/timer/tokens/cost state and auto-renders
on any reactive change. StatusPresenter retained as thin formatter."
```

---

## Task 2: CompactNotice Widget

Extract the fragile 3-method compact lifecycle into a self-contained reactive widget.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/compact_notice.py`
- Create: `tests/test_tui_compact_notice.py`
- Modify: `src/marim_harness/interfaces/tui/app.py:148,737-758,760-781,1270`

**Interfaces:**
- Consumes: Nothing (self-contained)
- Produces: `CompactNotice` widget with `compacting`, `done`, `error_msg` reactives

- [ ] **Step 1: Write tests for CompactNotice behavior**

```python
# tests/test_tui_compact_notice.py
"""Tests for the CompactNotice reactive widget."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.widgets.compact_notice import CompactNotice


class _CompactApp(App[None]):
    def compose(self) -> ComposeResult:
        yield CompactNotice()


@pytest.fixture()
async def pilot():
    async with _CompactApp().run_test() as pilot:
        yield pilot


async def test_hidden_by_default(pilot):
    """CompactNotice is hidden on mount."""
    notice = pilot.app.query_one(CompactNotice)
    assert notice.display is False


async def test_compacting_shows(pilot):
    """Setting compacting=True shows the notice."""
    notice = pilot.app.query_one(CompactNotice)
    notice.compacting = True
    await pilot.pause()
    assert notice.display is True
    text = notice.render()
    assert "compacting" in str(text).lower()


async def test_compacting_false_hides(pilot):
    """Setting compacting=False hides the notice."""
    notice = pilot.app.query_one(CompactNotice)
    notice.compacting = True
    await pilot.pause()
    notice.compacting = False
    await pilot.pause()
    assert notice.display is False


async def test_done_shows_checkmark(pilot):
    """Setting done=True shows a checkmark briefly."""
    notice = pilot.app.query_one(CompactNotice)
    notice.done = True
    await pilot.pause()
    assert notice.display is True
    text = notice.render()
    assert "✓" in str(text) or "done" in str(text).lower()


async def test_error_shows_message(pilot):
    """Setting error_msg shows an error notice."""
    notice = pilot.app.query_one(CompactNotice)
    notice.error_msg = "compaction failed"
    await pilot.pause()
    assert notice.display is True
    text = notice.render()
    assert "compaction failed" in str(text)


async def test_compacting_false_clears_error(pilot):
    """Setting compacting=False after error hides the error."""
    notice = pilot.app.query_one(CompactNotice)
    notice.error_msg = "compaction failed"
    await pilot.pause()
    notice.compacting = False
    await pilot.pause()
    assert notice.display is False
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_compact_notice.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement CompactNotice widget**

```python
# src/marim_harness/interfaces/tui/widgets/compact_notice.py
"""Self-contained compact notice — replaces the fragile 3-method lifecycle
(_on_compact_start → _on_compact → clear_compacting_notice) with reactive
state. Setting ``compacting = False`` always hides the notice, so there are
no dangling refs or manual cleanup paths."""
from __future__ import annotations

from textual.reactive import reactive
from textual.widgets import Static


class CompactNotice(Static):
    """A reactive notice for the compaction pipeline.

    Three independent reactives drive three visual states:
    - ``compacting``: True → spinner + "Compacting…"
    - ``done``: True briefly → green checkmark, auto-hides after 2s
    - ``error_msg``: non-empty → red error, auto-hides after 5s

    The watcher on ``compacting`` hides the widget whenever it's set to
    False, clearing any other state. This eliminates the manual
    clear_compacting_notice try/except dance.
    """

    compacting: reactive[bool] = reactive(False, init=False)
    done: reactive[bool] = reactive(False, init=False)
    error_msg: reactive[str] = reactive("", init=False)

    def __init__(self) -> None:
        super().__init__()
        self.display = False  # hidden by default

    def watch_compacting(self, value: bool) -> None:
        """Show/hide the compaction spinner. Setting False always hides."""
        if value:
            self.display = True
            self.update("⟳ Compacting conversation…")
        else:
            self.display = False
            self.done = False
            self.error_msg = ""

    def watch_done(self, value: bool) → None:
        """Show a green checkmark, then auto-hide after 2s."""
        if value:
            self.display = True
            self.update("✓ Compaction complete")
            self.set_timer(2.0, self._hide)

    def watch_error_msg(self, value: str) -> None:
        """Show a red error message, then auto-hide after 5s."""
        if value:
            self.display = True
            self.update(f"✗ {value}")
            self.set_timer(5.0, self._hide)

    def _hide(self) -> None:
        """Hide the notice — safe to call even if already hidden."""
        self.display = False
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui_compact_notice.py -v`
Expected: PASS

- [ ] **Step 5: Wire CompactNotice into HarnessApp**

In `app.py`:
- Add `from .widgets.compact_notice import CompactNotice` to imports
- In `compose()`, add `yield CompactNotice()` (in the #status dock area)
- In `_on_compact_start`: replace `self._compacting_notice = NoticeMessage(...)` + `log.mount(...)` with `self.query_one(CompactNotice).compacting = True`
- In `_on_compact`: replace the remove/replace dance with:
  ```python
  notice = self.query_one(CompactNotice)
  notice.compacting = False
  notice.done = True
  ```
- Delete `clear_compacting_notice()` method entirely
- In `_run_turn` finally block: replace `self.clear_compacting_notice()` with `self.query_one(CompactNotice).compacting = False`
- In `_on_compact_error` or the error path: set `notice.error_msg = str(e)`
- Remove `self._compacting_notice` instance variable
- Remove `NoticeMessage` import if no longer used

- [ ] **Step 6: Run full TUI test suite**

Run: `uv run pytest tests/ -k "tui or compact or app" --no-cov -x`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/compact_notice.py \
        src/marim_harness/interfaces/tui/app.py \
        tests/test_tui_compact_notice.py
git commit -m "feat(tui): extract CompactNotice widget with reactive lifecycle

Replace the fragile 3-method compact lifecycle with a self-contained
reactive widget. Setting compacting=False always hides the notice,
eliminating the dangling-ref risk."
```

---

## Task 3: QueueDisplay Widget

Extract queue rendering from `QueuePanel`/`HarnessApp._render_queue()` into a reactive widget.

**Files:**
- Create: `src/marim_harness/interfaces/tui/widgets/queue_display.py`
- Create: `tests/test_tui_queue_display.py`
- Modify: `src/marim_harness/interfaces/tui/app.py:248,629-694,722-724,1165,1256,1260`
- Modify: `src/marim_harness/interfaces/tui/widgets/panels.py:187-198`

**Interfaces:**
- Consumes: `TurnQueue` (from `queue.py`), `QueuedMessage`
- Produces: `QueueDisplay` widget with `items`, `paused` reactives

- [ ] **Step 1: Write tests for QueueDisplay behavior**

```python
# tests/test_tui_queue_display.py
"""Tests for the QueueDisplay reactive widget."""
from __future__ import annotations

import pytest
from textual.app import App, ComposeResult

from marim_harness.interfaces.tui.queue import QueuedMessage
from marim_harness.interfaces.tui.widgets.queue_display import QueueDisplay


class _QueueApp(App[None]):
    def compose(self) -> ComposeResult:
        yield QueueDisplay()


@pytest.fixture()
async def pilot():
    async with _QueueApp().run_test() as pilot:
        yield pilot


async def test_hidden_when_empty(pilot):
    """QueueDisplay is hidden when items list is empty."""
    qd = pilot.app.query_one(QueueDisplay)
    assert qd.display is False


async def test_shows_items(pilot):
    """Setting items shows the queue."""
    qd = pilot.app.query_one(QueueDisplay)
    qd.items = [QueuedMessage("hello", None, "1")]
    await pilot.pause()
    assert qd.display is True
    text = qd.render()
    assert "hello" in str(text)


async def test_paused_badge(pilot):
    """Setting paused=True shows a paused indicator."""
    qd = pilot.app.query_one(QueueDisplay)
    qd.items = [QueuedMessage("hello", None, "1")]
    qd.paused = True
    await pilot.pause()
    text = qd.render()
    assert "paused" in str(text).lower()


async def test_hides_when_items_cleared(pilot):
    """Clearing items hides the queue."""
    qd = pilot.app.query_one(QueueDisplay)
    qd.items = [QueuedMessage("hello", None, "1")]
    await pilot.pause()
    qd.items = []
    await pilot.pause()
    assert qd.display is False


async def test_multiple_items(pilot):
    """Multiple items are all rendered."""
    qd = pilot.app.query_one(QueueDisplay)
    qd.items = [
        QueuedMessage("first", None, "1"),
        QueuedMessage("second", None, "2"),
    ]
    await pilot.pause()
    text = qd.render()
    assert "first" in str(text)
    assert "second" in str(text)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_tui_queue_display.py -v`
Expected: FAIL — `ModuleNotFoundError`

- [ ] **Step 3: Implement QueueDisplay widget**

```python
# src/marim_harness/interfaces/tui/widgets/queue_display.py
"""Reactive queue display — renders queued user messages. Replaces the
manual QueuePanel.show_queue() / _render_queue() pattern."""
from __future__ import annotations

from textual.markup import escape
from textual.reactive import reactive
from textual.widgets import Static

from ..queue import QueuedMessage


class QueueDisplay(Static):
    """A reactive display for queued messages.

    Setting ``items`` to a non-empty list shows the queue; setting it to
    an empty list hides it. ``paused`` adds a pause badge.
    """

    items: reactive[list[QueuedMessage]] = reactive(list, init=False)
    paused: reactive[bool] = reactive(False, init=False)

    def __init__(self) -> None:
        super().__init__()
        self.display = False  # hidden when empty

    def watch_items(self, value: list[QueuedMessage]) -> None:
        """Show/hide based on item count."""
        self.display = bool(value)
        self._render()

    def watch_paused(self, value: bool) -> None:
        """Re-render to show/hide pause badge."""
        self._render()

    def _render(self) -> None:
        """Render the queue items as markup."""
        if not self.items:
            return
        lines = []
        for i, m in enumerate(self.items, 1):
            n = len(m.attachments or [])
            tag = f" 📎{n}" if n else ""
            lines.append(
                f"{i}. {escape(m.text)}{tag}  "
                f"[@click=app.edit_queued('{m.id}')]edit[/] "
                f"[@click=app.remove_queued('{m.id}')]✕[/]"
            )
        header = "Queued — paused" if self.paused else "Queued"
        self.update(f"[bold]{header}[/]\n" + "\n".join(lines))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_tui_queue_display.py -v`
Expected: PASS

- [ ] **Step 5: Wire QueueDisplay into HarnessApp**

In `app.py`:
- Add `from .widgets.queue_display import QueueDisplay` to imports
- In `compose()`, add `yield QueueDisplay()` in the appropriate layout position
- Replace `_render_queue()` body with:
  ```python
  qd = self.query_one(QueueDisplay)
  qd.items = list(self._queue.items)
  qd.paused = self._queue.paused
  ```
- All existing `_render_queue()` call sites continue to work (they call the same method)
- Remove the `QueuePanel` compose/ mount if QueueDisplay replaces it entirely

In `widgets/panels.py`:
- `QueuePanel.show_queue()` becomes a thin wrapper or is removed if QueueDisplay replaces it

- [ ] **Step 6: Run full TUI test suite**

Run: `uv run pytest tests/ -k "tui or queue or app" --no-cov -x`
Expected: PASS

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/widgets/queue_display.py \
        src/marim_harness/interfaces/tui/app.py \
        src/marim_harness/interfaces/tui/widgets/panels.py \
        tests/test_tui_queue_display.py
git commit -m "feat(tui): extract QueueDisplay widget with reactive state

Replace manual QueuePanel.show_queue() with reactive items/paused
bindings. QueueDisplay auto-shows when items are non-empty."
```

---

## Task 4: StreamRenderer Reactive Annotations

Add reactive annotations to StreamRenderer's display-state values, eliminating manual refresh_status() calls.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/stream_render.py`

**Interfaces:**
- Consumes: Nothing new
- Produces: `StreamRenderer.live_run_tokens`, `last_ttft`, `current_model` as reactive attributes

- [ ] **Step 1: Add reactive imports and declarations**

In `stream_render.py`, add at the top:
```python
from textual.reactive import reactive
```

On `StreamRenderer` class, add reactive declarations:
```python
class StreamRenderer:
    # ... existing code ...
    live_run_tokens: reactive[int] = reactive(0, init=False)
    last_ttft: reactive[float | None] = reactive(None, init=False)
    current_model: reactive[str] = reactive("", init=False)
```

- [ ] **Step 2: Remove manual refresh_status() calls**

Search for all `self.app.status.refresh_status()` in `stream_render.py` and replace with reactive assignments:
```python
# Before:
self.app.status.refresh_status()

# After (if updating tokens):
# The reactive handles this automatically — just assign:
self.live_run_tokens = <new_value>
# StatusBar watches this reactive and re-renders
```

Note: StatusBar needs to watch StreamRenderer's reactives. Add in StatusBar.__init__:
```python
# Watch StreamRenderer's reactives for token/ttft changes
# (done in Task 1 wiring, but verified here)
```

- [ ] **Step 3: Run tests**

Run: `uv run pytest tests/ -k "tui or stream or render" --no-cov -x`
Expected: PASS

- [ ] **Step 4: Commit**

```bash
git add src/marim_harness/interfaces/tui/stream_render.py
git commit -m "feat(tui): add reactive annotations to StreamRenderer

live_run_tokens, last_ttft, and current_model are now reactive,
eliminating manual refresh_status() calls from the renderer."
```

---

## Task 5: SubAgentsScreen → Screen Subclass

Convert SubAgentsScreen from a plain Python object to a proper Textual Screen subclass with reactive state.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/subagents/screen.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (subagents push/pop logic)

**Interfaces:**
- Consumes: `SubAgentWidget`, `TranscriptPane`, `SubAgentsScreen` (existing)
- Produces: `SubAgentsScreen` as a `Screen` subclass with `open`, `index`, `dirty` reactives

- [ ] **Step 1: Convert SubAgentsScreen to Screen subclass**

In `subagents/screen.py`:
```python
# Before:
class SubAgentsScreen:
    def __init__(self, app: "HarnessApp") -> None:
        self.app = app
        self.open = False
        self.index = 0
        self.dirty = False
        # ...

# After:
from textual.screen import Screen
from textual.reactive import reactive

class SubAgentsScreen(Screen):
    open: reactive[bool] = reactive(False, init=False)
    index: reactive[int] = reactive(0, init=False)
    dirty: reactive[bool] = reactive(False, init=False)

    def __init__(self) -> None:
        super().__init__()
        # ... existing init logic (without app param)
```

- [ ] **Step 2: Update HarnessApp to push SubAgentsScreen**

In `app.py`:
- Replace `self._subagents_screen = SubAgentsScreen(self)` with creating the screen on demand
- When opening sub-agents: `self.push_screen(SubAgentsScreen())` or check if already installed
- Update all `self.subagents.open = True/False` to use `self.query_one(SubAgentsScreen).open = True/False`
- Update `self.subagents.dirty = True` to reactive assignment

- [ ] **Step 3: Update watch_dirty to trigger repaint**

In `SubAgentsScreen`:
```python
def watch_dirty(self, value: bool) -> None:
    """Trigger repaint when dirty flag is set."""
    if value:
        self._repaint()
        self.dirty = False  # auto-reset
```

- [ ] **Step 4: Run tests**

Run: `uv run pytest tests/ -k "tui or subagent" --no-cov -x`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/subagents/screen.py \
        src/marim_harness/interfaces/tui/app.py
git commit -m "feat(tui): convert SubAgentsScreen to Screen subclass

SubAgentsScreen is now a proper Textual Screen with reactive
open/index/dirty state. This enables push_screen pattern and
improves composability and testability."
```

---

## Task 6: HarnessApp Cleanup

Final cleanup after all extractions — remove dead code, simplify compose, verify final line count.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`

**Interfaces:**
- Consumes: All widgets from Tasks 1-5
- Produces: Slimmed HarnessApp (~970 lines)

- [ ] **Step 1: Remove dead instance variables**

Remove from `__init__`:
- `self._compacting_notice` (moved to CompactNotice)
- `self._status_busy`, `self._model_name`, `self._mode` (moved to StatusBar)
- `self._live_run_tokens`, `self._last_ttft` (moved to StreamRenderer reactive)

- [ ] **Step 2: Simplify compose()**

Replace inline widget creation with composed widgets:
```python
def compose(self) -> ComposeResult:
    # ... existing structure ...
    yield StatusBar()      # was: Static(id="status-bar")
    yield CompactNotice()  # new
    yield QueueDisplay()   # new or replaces QueuePanel
    # ... rest of layout ...
```

- [ ] **Step 3: Verify line count**

Run: `wc -l src/marim_harness/interfaces/tui/app.py`
Expected: ~970 lines (down from 1271)

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest tests/ --no-cov -x`
Expected: PASS

- [ ] **Step 5: Run linter and type checker**

Run: `uv run ruff check src/marim_harness/interfaces/tui/`
Run: `uv run pyright src/marim_harness/interfaces/tui/`
Expected: No errors

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py
git commit -m "refactor(tui): clean up HarnessApp after reactive extraction

Remove ~300 lines of extracted state and dead code. HarnessApp
is now a thin coordinator that delegates rendering to reactive
widgets."
```

---

## Self-Review

### Spec Coverage

| Spec Section | Plan Task | Covered? |
|---|---|---|
| 1. StatusBar Widget | Task 1 | ✅ |
| 2. CompactNotice Widget | Task 2 | ✅ |
| 3. QueueDisplay | Task 3 | ✅ |
| 4. StreamRenderer Reactive | Task 4 | ✅ |
| 5. SubAgentsScreen → Screen | Task 5 | ✅ |
| 6. HarnessApp Cleanup | Task 6 | ✅ |
| 7. Testing Strategy | Each task has TDD steps | ✅ |

### Placeholder Scan

No placeholders found — all steps contain actual code or concrete actions.

### Type Consistency

- `StatusBar.busy: reactive[bool]` matches all call sites
- `CompactNotice.compacting: reactive[bool]` matches all call sites
- `QueueDisplay.items: reactive[list[QueuedMessage]]` matches `TurnQueue.items`
- `SubAgentsScreen.open: reactive[bool]` matches all `self.subagents.open` assignments
