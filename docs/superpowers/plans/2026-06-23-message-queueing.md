# Message Queueing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user submit a message while a turn is running; it is buffered into a visible, editable FIFO queue and run automatically as its own turn after the current one finishes cleanly.

**Architecture:** Entirely in the TUI layer (`interfaces/tui/`). A `QueuedMessage` list lives on `HarnessApp`; submitting while a turn runs appends to it instead of starting a worker. A drain step in `_run_turn`'s `finally` (next to the existing `_maybe_wake()`) starts the next queued item on clean completion; cancel/error sets a pause flag and skips draining. `Harness`, `Deps`, and the run loop are untouched — the queue just re-invokes the existing `run_turn`.

**Tech Stack:** Python 3, Textual (TUI: `App`, workers, `Static`/`Content` markup, `Pilot` test harness), pydantic-ai (`TestModel` in tests), pytest (`pytest-anyio`), `uv`.

## Global Constraints

- Run tests with `uv run pytest`.
- No changes to `Harness`, `Deps`, or the run loop. Queueing is a TUI-only concern.
- Items run strictly FIFO, each as its own `run_turn` call, never concurrently — only spawn the next worker after the previous finished (preserve the existing `exclusive=True` worker invariant).
- The queue is in-memory / process-scoped (like `jobs`); it is dropped on quit / session-switch / `/clear`. Not persisted.
- A drained queue item is user-initiated work: `_start_turn` resets `_auto_turn_depth = 0` (breaks the autonomous-wake chain, same as a typed submit) — so a drain never counts against the wake depth cap.
- `Alt+Enter` (steer) is reserved for a later spec and is NOT wired here.
- Existing empty/whitespace and `/command` handling in `on_prompt_input_submitted` is preserved: empty input and slash-commands never enter the queue.

## File Structure

- `src/marim_harness/interfaces/tui/queue.py` — **new.** `QueuedMessage` dataclass + `render_queue(items) -> str` renderer (markup string). One responsibility: the queue's data shape and how it renders.
- `src/marim_harness/interfaces/tui/widgets/panels.py` — **modify.** Add a `markup` flag to `LivePanel` and a `QueuePanel` subclass (mirrors `TaskPanel`/`JobPanel`).
- `src/marim_harness/interfaces/tui/app.py` — **modify.** Queue state, `_start_turn` extraction, enqueue branch, drain/pause/resume, panel render hook, binding + actions.
- `tests/test_queue.py` — **new.** Unit + Pilot tests for the queue.

---

### Task 1: Core queue mechanics + visible (read-only) panel + resume

Buffer-while-busy, FIFO drain on clean completion, pause on cancel/error, manual resume, and a visible panel listing the pending items. No per-item controls yet (Task 2).

**Files:**
- Create: `src/marim_harness/interfaces/tui/queue.py`
- Modify: `src/marim_harness/interfaces/tui/widgets/panels.py` (add `markup` flag to `LivePanel`; add `QueuePanel`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (state, `_start_turn`, enqueue branch, drain/pause/resume, render hook, binding)
- Create: `tests/test_queue.py`

**Interfaces:**
- Produces `QueuedMessage(text: str, attachments: Optional[list[tuple[bytes, str]]], id: str)` (a dataclass) in `marim_harness.interfaces.tui.queue`.
- Produces `render_queue(items: list[QueuedMessage]) -> str` in the same module (returns a Textual-markup string; user text escaped).
- Produces `QueuePanel` in `marim_harness.interfaces.tui.widgets.panels`, with `show_queue(items: list, paused: bool = False) -> None`.
- Produces on `HarnessApp`: `self._queue: list[QueuedMessage]`, `self._queue_paused: bool`, `self._queue_seq: int`; async `_start_turn(text, attachments=None)`, `_enqueue(text, attachments=None)`, async `_drain_next()`, async `_after_turn()`, `_render_queue()`, async `action_run_queued()`.

- [ ] **Step 1: Write the failing test for `QueuedMessage` + `render_queue`**

Create `tests/test_queue.py`:

```python
from marim_harness.interfaces.tui.queue import QueuedMessage, render_queue


def test_queued_message_holds_text_attachments_id():
    m = QueuedMessage("hello", None, "1")
    assert m.text == "hello"
    assert m.attachments is None
    assert m.id == "1"


def test_render_queue_lists_items_in_order():
    items = [QueuedMessage("first", None, "1"), QueuedMessage("second", None, "2")]
    out = render_queue(items)
    assert "1. first" in out
    assert "2. second" in out
    # first appears before second
    assert out.index("first") < out.index("second")


def test_render_queue_shows_attachment_count():
    items = [QueuedMessage("with files", [(b"x", "image/png"), (b"y", "image/png")], "1")]
    assert "📎2" in render_queue(items)


def test_render_queue_escapes_markup_in_user_text():
    # A '[' in user text must not be parsed as Textual markup.
    items = [QueuedMessage("do [this]", None, "1")]
    out = render_queue(items)
    assert "\\[this]" in out  # escaped open bracket
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_queue.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.queue'`.

- [ ] **Step 3: Create `queue.py`**

Create `src/marim_harness/interfaces/tui/queue.py`:

```python
"""The TUI message queue: messages the user submitted while a turn was running,
held to run as their own turns after the current one. In-memory, process-scoped."""

from dataclasses import dataclass
from typing import Optional

from textual.markup import escape


@dataclass
class QueuedMessage:
    """One buffered user submission. ``attachments`` mirrors the tuple list
    ``Harness.run_turn`` accepts; ``id`` is a stable, per-app sequence string
    used to target the item from the panel's controls."""

    text: str
    attachments: Optional[list[tuple[bytes, str]]]
    id: str


def render_queue(items: list) -> str:
    """Render the pending items as a numbered Textual-markup string. User text
    is escaped so brackets in a prompt are not parsed as markup."""
    lines = []
    for i, m in enumerate(items, 1):
        n = len(m.attachments or [])
        tag = f" 📎{n}" if n else ""
        lines.append(f"{i}. {escape(m.text)}{tag}")
    return "\n".join(lines)
```

- [ ] **Step 4: Run the test to verify it passes**

Run: `uv run pytest tests/test_queue.py -v`
Expected: PASS.

- [ ] **Step 5: Add the `markup` flag + `QueuePanel` to `panels.py`**

In `src/marim_harness/interfaces/tui/widgets/panels.py`, change `LivePanel.__init__` to accept a `markup` flag, store it, and use it in `_render_items`.

Change the `__init__` signature (currently `def __init__(self, *, name: str, title: str, renderer: Callable[[list], str]) -> None:`) to:

```python
    def __init__(self, *, name: str, title: str, renderer: Callable[[list], str],
                 markup: bool = False) -> None:
```

and add `self._markup = markup` alongside the other assignments in that method.

Then change the last line of `_render_items` (currently `self._body.update(Content(self._renderer(items)))`) to:

```python
        text = self._renderer(items)
        self._body.update(Content.from_markup(text) if self._markup else Content(text))
```

Add the `QueuePanel` subclass at the end of the file (mirrors `TaskPanel`/`JobPanel`, but renders markup and sets a paused title):

```python
class QueuePanel(LivePanel):
    """Messages queued to run after the current turn."""

    def __init__(self) -> None:
        from ..queue import render_queue

        super().__init__(name="queue", title="Queued", renderer=render_queue,
                         markup=True)

    def show_queue(self, items: list, paused: bool = False) -> None:
        self._title = "Queued — paused" if paused else "Queued"
        self._render_items(items)
```

- [ ] **Step 6: Write the failing test for enqueue-while-busy + idle-submit-unchanged**

Add to `tests/test_queue.py`:

```python
from pathlib import Path

import pytest

from marim_harness.deps import Deps
from marim_harness.interfaces.tui.app import HarnessApp
from marim_harness.interfaces.tui.widgets.prompt import PromptInput
from marim_harness.permissions import Mode


@pytest.fixture
def anyio_backend():
    return "asyncio"


def _app(tmp_path: Path) -> HarnessApp:
    from pydantic_ai.models.test import TestModel

    from marim_harness.agent import Harness
    from marim_harness.tools.provider import BuiltinToolProvider

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = Harness(
        TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test"
    )
    return HarnessApp(harness)


@pytest.mark.anyio
async def test_submit_while_busy_enqueues(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        sentinel = object()
        app._turn_worker = sentinel  # simulate a running turn
        await app.on_prompt_input_submitted(PromptInput.Submitted("queued one", []))
        assert [m.text for m in app._queue] == ["queued one"]
        assert app._turn_worker is sentinel  # no new worker started


@pytest.mark.anyio
async def test_idle_submit_runs_immediately(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_worker = None
        await app.on_prompt_input_submitted(PromptInput.Submitted("hello", []))
        assert app._queue == []
        assert app._turn_worker is not None  # a worker was spawned
```

- [ ] **Step 7: Run to verify failure**

Run: `uv run pytest tests/test_queue.py::test_submit_while_busy_enqueues -v`
Expected: FAIL — `AttributeError: 'HarnessApp' object has no attribute '_queue'`.

- [ ] **Step 8: Wire queue state, `_start_turn`, and the enqueue branch in `app.py`**

In `src/marim_harness/interfaces/tui/app.py`:

(a) Add the import near the other widget imports:

```python
from .queue import QueuedMessage
from .widgets.panels import JobPanel, QueuePanel, TaskPanel
```

(adjust to merge with the existing `panels` import — `QueuePanel` joins `JobPanel`/`TaskPanel`).

(b) In `__init__`, after `self._turn_worker = None` (line ~98), add:

```python
        self._queue: list[QueuedMessage] = []
        self._queue_paused = False
        self._queue_seq = 0
```

(c) In `compose()`, add the panel after `yield TaskPanel()`:

```python
        yield QueuePanel()
```

(d) In `on_mount()`, after `self._render_jobs()` (line ~151), add:

```python
        self._render_queue()
```

(e) Replace the tail of `on_prompt_input_submitted` (the block currently starting at `log = self.query_one("#log", VerticalScroll)` / `await log.mount(UserMessage(text))` / `self.stream.current_assistant = None` / `self._auto_turn_depth = 0` / `self._turn_worker = self.run_worker(...)`) with the busy branch + a call to the extracted helper:

```python
        if self._turn_worker is not None:
            self._enqueue(text, event.attachments)
            return
        await self._start_turn(text, event.attachments)
```

(f) Add these methods to `HarnessApp` (place them next to `_run_turn`):

```python
    async def _start_turn(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Mount the user message and spawn the exclusive turn worker. Shared by
        a fresh submit and a drained queue item. Resets the autonomous-wake
        chain and clears any queue pause (a user-driven turn resumes draining)."""
        self._queue_paused = False
        self._auto_turn_depth = 0
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self.stream.current_assistant = None
        self._turn_worker = self.run_worker(
            self._run_turn(text, attachments), exclusive=True
        )

    def _enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._queue_seq += 1
        self._queue.append(QueuedMessage(text, attachments, str(self._queue_seq)))
        self._render_queue()

    async def _drain_next(self) -> None:
        """Pop and start the next queued message."""
        item = self._queue.pop(0)
        self._render_queue()
        await self._start_turn(item.text, item.attachments)

    async def _after_turn(self) -> None:
        """Called from _run_turn's finally. Drain the next queued item on a
        clean, unpaused turn; otherwise fall through to the background-job wake."""
        if not self._queue_paused and self._queue:
            await self._drain_next()
        else:
            self._maybe_wake()

    def _render_queue(self) -> None:
        """Repaint the queue panel from the current queue."""
        if not self.is_running:
            return
        try:
            panel = self.query_one(QueuePanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        panel.show_queue(self._queue, paused=self._queue_paused)
```

- [ ] **Step 9: Run the enqueue/idle tests to verify they pass**

Run: `uv run pytest tests/test_queue.py::test_submit_while_busy_enqueues tests/test_queue.py::test_idle_submit_runs_immediately -v`
Expected: PASS.

- [ ] **Step 10: Write the failing tests for drain, pause, and resume**

Add to `tests/test_queue.py`:

```python
import asyncio
from asyncio import CancelledError

from marim_harness.interfaces.tui.queue import QueuedMessage


@pytest.mark.anyio
async def test_after_turn_drains_next_when_not_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app._start_turn = fake_start
        app._queue = [QueuedMessage("a", None, "1"), QueuedMessage("b", None, "2")]
        app._queue_paused = False
        await app._after_turn()
        assert started == ["a"]
        assert [m.text for m in app._queue] == ["b"]


@pytest.mark.anyio
async def test_after_turn_does_not_drain_when_paused(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        woke = []
        app._maybe_wake = lambda: woke.append(True)
        app._queue = [QueuedMessage("a", None, "1")]
        app._queue_paused = True
        await app._after_turn()
        assert [m.text for m in app._queue] == ["a"]  # untouched
        assert woke == [True]  # fell through to wake


@pytest.mark.anyio
async def test_error_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise RuntimeError("boom")

        app.harness.run_turn = boom
        app._queue = [QueuedMessage("a", None, "1")]
        await app._run_turn("x")  # caught by the except Exception branch
        assert app._queue_paused is True
        assert [m.text for m in app._queue] == ["a"]


@pytest.mark.anyio
async def test_cancel_pauses_queue(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()

        async def boom(*a, **k):
            raise CancelledError()

        app.harness.run_turn = boom
        app._queue = [QueuedMessage("a", None, "1")]
        with pytest.raises(CancelledError):
            await app._run_turn("x")
        assert app._queue_paused is True


@pytest.mark.anyio
async def test_run_queued_action_resumes(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        started = []

        async def fake_start(text, attachments=None):
            started.append(text)

        app._start_turn = fake_start
        app._turn_worker = None
        app._queue_paused = True
        app._queue = [QueuedMessage("a", None, "1")]
        await app.action_run_queued()
        assert app._queue_paused is False
        assert started == ["a"]
```

- [ ] **Step 11: Run to verify failure**

Run: `uv run pytest tests/test_queue.py -k "drains or pauses or resumes" -v`
Expected: FAIL — `_after_turn` / `action_run_queued` not defined, and `_queue_paused` not set on error/cancel.

- [ ] **Step 12: Wire drain into `_run_turn`'s finally, pause on cancel/error, and add the resume action + binding**

In `src/marim_harness/interfaces/tui/app.py`:

(a) In `_run_turn`, in the `except CancelledError:` branch, add `self._queue_paused = True` before the `raise`:

```python
        except CancelledError:
            self._queue_paused = True
            log.mount(ErrorMessage("turn cancelled"))
            raise
```

(b) In the `except Exception as exc:` branch, add `self._queue_paused = True` (e.g. as the first line of the branch).

(c) In the `finally:` block, replace `self._maybe_wake()` with:

```python
            await self._after_turn()  # drain next queued item, or wake on jobs
```

(d) Add the binding to `BINDINGS`:

```python
        ("ctrl+r", "run_queued", "Run queued"),
```

(e) Add the action method (next to `action_cancel_turn`):

```python
    async def action_run_queued(self) -> None:
        """Resume a paused queue: clear the pause and start the next item."""
        if self._queue and self._turn_worker is None:
            self._queue_paused = False
            await self._drain_next()
```

- [ ] **Step 13: Run the full queue test file**

Run: `uv run pytest tests/test_queue.py -v`
Expected: PASS (all Task 1 tests).

- [ ] **Step 14: Run the full suite to confirm no regressions**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS — prior count plus the new tests, no failures. Watch `test_app.py` (the submit/worker/wake paths changed via the `_start_turn` extraction and the `finally` swap).

- [ ] **Step 15: Commit**

```bash
git add src/marim_harness/interfaces/tui/queue.py src/marim_harness/interfaces/tui/widgets/panels.py src/marim_harness/interfaces/tui/app.py tests/test_queue.py
git commit -m "feat(tui): message queue — buffer submits during a turn, drain FIFO after"
```

---

### Task 2: Interactive panel — per-item remove and edit

Add clickable `edit` / `✕` controls to each queued row and the app actions behind them. Edit pops the item's text back into the prompt input; remove drops it.

**Files:**
- Modify: `src/marim_harness/interfaces/tui/queue.py` (`render_queue` emits `@click` action links)
- Modify: `src/marim_harness/interfaces/tui/app.py` (`action_remove_queued`, async `action_edit_queued`)
- Modify: `tests/test_queue.py` (add tests)

**Interfaces:**
- Consumes from Task 1: `QueuedMessage`, `render_queue`, `self._queue`, `self._render_queue()`, and `PromptInput` (`.text`, `.move_cursor`, `.document.end`, `.focus()`).
- Produces on `HarnessApp`: `action_remove_queued(self, id: str) -> None` and async `action_edit_queued(self, id: str) -> None`.

- [ ] **Step 1: Write the failing test for the click-link markup**

Add to `tests/test_queue.py`:

```python
def test_render_queue_embeds_click_actions():
    items = [QueuedMessage("draft one", None, "7")]
    out = render_queue(items)
    assert "@click=app.edit_queued('7')" in out
    assert "@click=app.remove_queued('7')" in out
```

- [ ] **Step 2: Run to verify failure**

Run: `uv run pytest tests/test_queue.py::test_render_queue_embeds_click_actions -v`
Expected: FAIL — the markup has no `@click` spans yet.

- [ ] **Step 3: Add click-link markup to `render_queue`**

In `src/marim_harness/interfaces/tui/queue.py`, replace the loop body of `render_queue` so each line ends with two action links (ids are numeric strings, safe to inline in markup):

```python
def render_queue(items: list) -> str:
    """Render the pending items as a numbered Textual-markup string with
    per-item edit/remove action links. User text is escaped so brackets in a
    prompt are not parsed as markup; the ids are numeric and safe inline."""
    lines = []
    for i, m in enumerate(items, 1):
        n = len(m.attachments or [])
        tag = f" 📎{n}" if n else ""
        lines.append(
            f"{i}. {escape(m.text)}{tag}  "
            f"[@click=app.edit_queued('{m.id}')]edit[/] "
            f"[@click=app.remove_queued('{m.id}')]✕[/]"
        )
    return "\n".join(lines)
```

- [ ] **Step 4: Run the markup test (and the earlier render tests still pass)**

Run: `uv run pytest tests/test_queue.py -k render_queue -v`
Expected: PASS — including `test_render_queue_escapes_markup_in_user_text` and `test_render_queue_lists_items_in_order` (the `1. first` / ordering assertions still hold).

- [ ] **Step 5: Write the failing tests for remove + edit actions**

Add to `tests/test_queue.py`:

```python
@pytest.mark.anyio
async def test_remove_queued_drops_item(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("a", None, "1"), QueuedMessage("b", None, "2")]
        app.action_remove_queued("1")
        assert [m.id for m in app._queue] == ["2"]


@pytest.mark.anyio
async def test_edit_queued_pops_text_into_input(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("draft me", None, "1")]
        await app.action_edit_queued("1")
        assert app._queue == []  # removed from the queue
        assert app.query_one(PromptInput).text == "draft me"


@pytest.mark.anyio
async def test_edit_queued_unknown_id_is_noop(tmp_path):
    app = _app(tmp_path)
    async with app.run_test() as pilot:
        await pilot.pause()
        app._queue = [QueuedMessage("a", None, "1")]
        await app.action_edit_queued("nope")
        assert [m.id for m in app._queue] == ["1"]
```

- [ ] **Step 6: Run to verify failure**

Run: `uv run pytest tests/test_queue.py -k "remove_queued or edit_queued" -v`
Expected: FAIL — `action_remove_queued` / `action_edit_queued` not defined.

- [ ] **Step 7: Add the remove + edit actions to `app.py`**

In `src/marim_harness/interfaces/tui/app.py`, add (next to `action_run_queued`):

```python
    def action_remove_queued(self, id: str) -> None:
        """Drop a pending queued message before it runs."""
        self._queue = [m for m in self._queue if m.id != id]
        self._render_queue()

    async def action_edit_queued(self, id: str) -> None:
        """Pop a queued message out of the queue and load its text into the
        prompt input for editing. Attachments are not restored — editing is
        text-only; re-add attachments before resubmitting if needed."""
        item = next((m for m in self._queue if m.id == id), None)
        if item is None:
            return
        self._queue = [m for m in self._queue if m.id != id]
        self._render_queue()
        prompt = self.query_one(PromptInput)
        prompt.text = item.text
        prompt.move_cursor(prompt.document.end)
        prompt.focus()
```

Ensure `PromptInput` is imported in `app.py` (it is — the existing autocomplete handler uses `self.query_one(PromptInput)`).

- [ ] **Step 8: Run the action tests**

Run: `uv run pytest tests/test_queue.py -k "remove_queued or edit_queued" -v`
Expected: PASS.

- [ ] **Step 9: Run the full suite**

Run: `uv run pytest --no-header -q -o addopts=""`
Expected: PASS, no regressions.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/interfaces/tui/queue.py src/marim_harness/interfaces/tui/app.py tests/test_queue.py
git commit -m "feat(tui): queue panel — clickable edit/remove for pending messages"
```

---

## Self-Review

**Spec coverage:**
- "Submit while busy → buffer into FIFO queue" → Task 1 enqueue branch + `_enqueue`. ✔
- "Visible list" → Task 1 `QueuePanel` + `_render_queue`. ✔
- "Editable / removable" → Task 2 `action_edit_queued` / `action_remove_queued` + click links. ✔
- "Each runs as its own turn, in order, after current finishes" → Task 1 `_after_turn` → `_drain_next` → `_start_turn`, FIFO `pop(0)`. ✔
- "Pause on cancel OR error; keep intact; manual resume" → Task 1 `_queue_paused` set in both `except` branches; `action_run_queued` resumes. ✔
- "Drop on teardown/session-switch (process-scoped)" → in-memory list, never persisted; no persistence code added. ✔ (Global Constraints.)
- "Drained item doesn't count against wake depth cap" → `_start_turn` resets `_auto_turn_depth = 0`. ✔
- "Queue drain takes priority over `_maybe_wake`" → `_after_turn` drains first, wakes only when queue empty/paused. ✔
- "Attachments carried through" → `QueuedMessage.attachments` → `_start_turn` → `run_turn`. ✔
- "Empty/whitespace and `/commands` never queue" → enqueue branch sits after the existing `if not text` / `if text.startswith("/")` / image-block checks. ✔
- "Edit re-queues at end / edit is text-only (attachments dropped)" → documented in `action_edit_queued` docstring and accepted in the spec. ✔
- "`Alt+Enter` reserved, not wired" → no Alt+Enter binding added. ✔

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows complete code; every test step shows the test.

**Type consistency:** `QueuedMessage(text, attachments, id)` field order and names match across `queue.py`, every `QueuedMessage(...)` test construction, and the `_enqueue` call. `render_queue`, `QueuePanel.show_queue(items, paused)`, `_render_queue`, `_after_turn`, `_drain_next`, `_start_turn`, `action_run_queued`, `action_remove_queued`, `action_edit_queued` are named identically in their definitions, call sites, and the `@click=app.edit_queued/remove_queued` markup. The `markup` flag added to `LivePanel.__init__` is consumed in `_render_items` and passed by `QueuePanel`.
