# HarnessApp Decomposition Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move three inline state machines off `HarnessApp` into Textual-free collaborator objects (`TurnQueue`, `FinishedJobNotifier`, `SubAgentViewer` + a `spend_tag` helper) so their logic is unit-testable in isolation, with zero behavior change.

**Architecture:** Each new object owns *state + decisions* and imports nothing from Textual; the App keeps every *effect* (widget queries/mounts, worker spawning, focus, desktop notifications, panel repaints). This mirrors the existing `WakeController` (`wake.py`) split. Existing App-level tests are the behavior-preserving regression net.

**Tech Stack:** Python ≥3.10, Pydantic AI, Textual, pytest (coverage on by default), ruff (E,F,I; line length 100), pyright (basic, src only). Tooling via `uv`.

## Global Constraints

- Use `uv` for everything: `uv run pytest`, `uv run ruff check src tests`, `uv run pyright`. Never bare `python`/`pytest`/`pip`.
- Gate every commit with CI order: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- `requires-python >=3.10` — no 3.11+ only syntax.
- Ruff line length 100; imports sorted (I rule).
- New collaborator objects MUST NOT import from `textual` — that is what makes them unit-testable without an App harness.
- Pure structural refactor: **no behavior change**. Existing tests must stay green untouched.
- Work happens on branch `refactor/harnessapp-decomposition` (already created; the spec is committed there).

---

### Task 1: `TurnQueue` — extract the buffered-submission state machine

**Files:**
- Modify: `src/marim_harness/interfaces/tui/queue.py` (add `TurnQueue` beside the existing `QueuedMessage` + `render_queue`)
- Modify: `src/marim_harness/interfaces/tui/app.py` (rewire all `_queue` / `_queue_seq` / `_queue_paused` touch points)
- Test: `tests/test_turn_queue.py` (new)

**Interfaces:**
- Consumes: `QueuedMessage(text: str, attachments: list[tuple[bytes, str]] | None, id: str)` from `queue.py`.
- Produces:
  - `TurnQueue()` — no args.
  - `.items -> list[QueuedMessage]` (read-only property)
  - `.paused: bool` (plain attribute, App flips it)
  - `.__bool__() -> bool` (truthy when non-empty)
  - `.enqueue(text: str, attachments: list[tuple[bytes, str]] | None = None) -> None`
  - `.prepend(text: str, attachments: list[tuple[bytes, str]] | None = None) -> None`
  - `.pop_next() -> QueuedMessage`
  - `.remove(id: str) -> None`
  - `.take(id: str) -> QueuedMessage | None`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_turn_queue.py`:

```python
from marim_harness.interfaces.tui.queue import QueuedMessage, TurnQueue


def test_enqueue_appends_in_order_with_monotonic_ids():
    q = TurnQueue()
    q.enqueue("a")
    q.enqueue("b", [(b"img", "png")])
    assert [m.text for m in q.items] == ["a", "b"]
    assert [m.id for m in q.items] == ["1", "2"]
    assert q.items[1].attachments == [(b"img", "png")]


def test_prepend_inserts_at_front_and_keeps_seq_monotonic():
    q = TurnQueue()
    q.enqueue("first")
    q.prepend("jumped")
    assert [m.text for m in q.items] == ["jumped", "first"]
    # prepend still advances the sequence — ids never collide with enqueue's.
    assert q.items[0].id == "2"


def test_prepend_multiple_in_reversed_loop_preserves_original_order():
    # Mirrors _after_turn: leftover steers [s1, s2] re-inserted via
    # `for x in reversed(leftover): prepend(x)` must end up [s1, s2] at the front.
    q = TurnQueue()
    q.enqueue("queued")
    for text in reversed(["s1", "s2"]):
        q.prepend(text)
    assert [m.text for m in q.items] == ["s1", "s2", "queued"]


def test_pop_next_returns_and_removes_front():
    q = TurnQueue()
    q.enqueue("a")
    q.enqueue("b")
    item = q.pop_next()
    assert item.text == "a"
    assert [m.text for m in q.items] == ["b"]


def test_remove_drops_by_id_and_is_noop_for_absent():
    q = TurnQueue()
    q.enqueue("a")  # id "1"
    q.enqueue("b")  # id "2"
    q.remove("1")
    assert [m.text for m in q.items] == ["b"]
    q.remove("999")  # absent — no error, no change
    assert [m.text for m in q.items] == ["b"]


def test_take_pops_specific_id_and_returns_none_when_absent():
    q = TurnQueue()
    q.enqueue("a")  # id "1"
    q.enqueue("b")  # id "2"
    taken = q.take("2")
    assert taken is not None and taken.text == "b"
    assert [m.text for m in q.items] == ["a"]
    assert q.take("2") is None


def test_bool_reflects_emptiness_and_paused_defaults_false():
    q = TurnQueue()
    assert not q
    assert q.paused is False
    q.enqueue("a")
    assert q
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_turn_queue.py -v`
Expected: FAIL — `ImportError: cannot import name 'TurnQueue'`.

- [ ] **Step 3: Implement `TurnQueue` in `queue.py`**

Append to `src/marim_harness/interfaces/tui/queue.py` (after `render_queue`):

```python
class TurnQueue:
    """The in-memory queue of user submissions buffered while a turn is running,
    held to run as their own turns afterward. Owns the ordering, the stable
    per-app id sequence, and the paused flag; the App performs the effects
    (panel repaint, draining a popped item into a turn worker). Free of Textual
    so the queue logic is unit-testable without an App."""

    def __init__(self) -> None:
        self._items: list[QueuedMessage] = []
        # Monotonic across enqueue AND prepend so a re-inserted steer never
        # collides with a pending item's id — the panel targets items by id.
        self._seq = 0
        # Flipped by the App on cancel/error so a drained turn waits for an
        # explicit resume; lives here because every queue read needs it.
        self.paused = False

    @property
    def items(self) -> list[QueuedMessage]:
        return self._items

    def __bool__(self) -> bool:
        return bool(self._items)

    def enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._seq += 1
        self._items.append(QueuedMessage(text, attachments, str(self._seq)))

    def prepend(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Re-insert a submission at the FRONT so it runs next — used for steers
        that landed in the turn-finishing gap and fall back to the queue."""
        self._seq += 1
        self._items.insert(0, QueuedMessage(text, attachments, str(self._seq)))

    def pop_next(self) -> QueuedMessage:
        """Remove and return the front item (the next to run)."""
        return self._items.pop(0)

    def remove(self, id: str) -> None:
        """Drop a pending item by id; a no-op if the id is absent."""
        self._items = [m for m in self._items if m.id != id]

    def take(self, id: str) -> QueuedMessage | None:
        """Pop a specific item out of the queue and return it, or None if the id
        is not present (used to load a queued message back into the prompt)."""
        item = next((m for m in self._items if m.id == id), None)
        if item is not None:
            self._items = [m for m in self._items if m.id != id]
        return item
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_turn_queue.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Rewire `HarnessApp.__init__`**

In `src/marim_harness/interfaces/tui/app.py`, replace the three queue attributes (currently lines 118-120):

```python
        self._queue: list[QueuedMessage] = []
        self._queue_paused = False
        self._queue_seq = 0
```

with:

```python
        self._queue = TurnQueue()
```

Update the queue import (currently `app.py:20`): change `from .queue import QueuedMessage` to `from .queue import QueuedMessage, TurnQueue`.

- [ ] **Step 6: Rewire the queue method bodies**

`_enqueue` (currently lines 511-517) — body becomes:

```python
    def _enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._queue.enqueue(text, attachments)
        self._render_queue()
```

`_drain_next` (519-523):

```python
    async def _drain_next(self) -> None:
        """Pop and start the next queued message."""
        item = self._queue.pop_next()
        self._render_queue()
        await self._start_turn(item.text, item.attachments)
```

`_after_turn` (525-550) — the leftover-steer loop and the drain guard. Replace the loop body and the two `_queue_paused` references:

```python
        leftover = self.harness.take_buffered_steers()
        if leftover:
            for text, atts in reversed(leftover):
                self._queue.prepend(text, atts)
            self._render_queue()
        try:
            if not self._queue.paused and self._queue:
                await self._drain_next()
            else:
                self._maybe_wake()
        except Exception as exc:
            self._queue.paused = True
            self._append_log(ErrorMessage(f"failed to start next turn: {exc}"))
```

`_render_queue` (552-560) — the panel feed (line 560):

```python
        panel.show_queue(self._queue.items, paused=self._queue.paused)
```

`action_run_queued` (562-566):

```python
    async def action_run_queued(self) -> None:
        """Resume a paused queue: clear the pause and start the next item."""
        if self._queue and not self.turn_busy:
            self._queue.paused = False
            await self._drain_next()
```

`action_remove_queued` (568-571):

```python
    def action_remove_queued(self, id: str) -> None:
        """Drop a pending queued message before it runs."""
        self._queue.remove(id)
        self._render_queue()
```

`action_edit_queued` (573-583+) — replace the find/return/filter prologue. The original is:

```python
        item = next((m for m in self._queue if m.id == id), None)
        if item is None:
            return
        self._queue = [m for m in self._queue if m.id != id]
        self._render_queue()
```

becomes:

```python
        item = self._queue.take(id)
        if item is None:
            return
        self._render_queue()
```

(Leave everything after `self._render_queue()` in that method — the prompt-loading code — unchanged.)

- [ ] **Step 7: Rewire the three remaining `_queue_paused` touch points**

These are outside the queue methods and easy to miss. Replace each `self._queue_paused` with `self._queue.paused`:

- `on_prompt_input_submitted` — line 913: `self._queue_paused = False` → `self._queue.paused = False`
- `_run_turn` CancelledError arm — line 939: `self._queue_paused = True` → `self._queue.paused = True`
- `_run_turn` Exception arm — line 943: `self._queue_paused = True` → `self._queue.paused = True`

- [ ] **Step 8: Verify no stale references remain**

Run: `grep -nE "_queue_paused|_queue_seq|self\._queue\.append|self\._queue\.pop|self\._queue\.insert|for m in self\._queue\b" src/marim_harness/interfaces/tui/app.py`
Expected: no output. (`self._queue` should now only appear as `self._queue.items`, `self._queue.enqueue/prepend/pop_next/remove/take`, `self._queue.paused`, or bare truthiness `self._queue`.)

- [ ] **Step 9: Run the gate**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: PASS. In particular `tests/test_queue.py` and `tests/test_app_decomposition.py` stay green untouched, proving behavior is preserved.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/interfaces/tui/queue.py src/marim_harness/interfaces/tui/app.py tests/test_turn_queue.py
git commit -m "refactor(tui): extract TurnQueue from HarnessApp

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013z5J2JSck7fQBLSyP9B4Eb"
```

---

### Task 2: `FinishedJobNotifier` — extract the notify-once dedup

**Files:**
- Create: `src/marim_harness/interfaces/tui/notify.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (`__init__` + `_notify_finished_jobs`)
- Test: `tests/test_finished_job_notifier.py` (new)

**Interfaces:**
- Consumes: `Job` objects from `marim_harness.jobs` (fields used: `.id: str`, `.kind: str`, `.status` ∈ `{"running","done","failed","cancelled"}`).
- Produces:
  - `FinishedJobNotifier()` — no args.
  - `.notified: set[str]`
  - `.newly_finished(jobs: Iterable[Job]) -> list[Job]` — returns each done/failed job whose id is unseen, adding those ids to `.notified`; cancelled/running never returned.

- [ ] **Step 1: Write the failing tests**

Create `tests/test_finished_job_notifier.py`:

```python
from dataclasses import dataclass

from marim_harness.interfaces.tui.notify import FinishedJobNotifier


@dataclass
class _FakeJob:
    id: str
    kind: str
    status: str


def test_returns_done_and_failed_once_each():
    n = FinishedJobNotifier()
    jobs = [
        _FakeJob("a", "bash", "done"),
        _FakeJob("b", "agent", "failed"),
        _FakeJob("c", "bash", "running"),
    ]
    fresh = n.newly_finished(jobs)
    assert {j.id for j in fresh} == {"a", "b"}  # running excluded


def test_each_job_notified_only_once_across_calls():
    n = FinishedJobNotifier()
    jobs = [_FakeJob("a", "bash", "done")]
    assert [j.id for j in n.newly_finished(jobs)] == ["a"]
    # Same job, polled again after it's already been notified.
    assert n.newly_finished(jobs) == []


def test_cancelled_jobs_are_never_returned():
    n = FinishedJobNotifier()
    jobs = [_FakeJob("a", "agent", "cancelled")]
    assert n.newly_finished(jobs) == []
    assert "a" not in n.notified


def test_running_then_done_returned_when_it_finishes():
    n = FinishedJobNotifier()
    job = _FakeJob("a", "bash", "running")
    assert n.newly_finished([job]) == []
    job.status = "done"
    assert [j.id for j in n.newly_finished([job])] == ["a"]
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_finished_job_notifier.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.notify'`.

- [ ] **Step 3: Implement `notify.py`**

Create `src/marim_harness/interfaces/tui/notify.py`:

```python
"""Desktop-notification dedup for finished background jobs.

Tracks which completed jobs have already been pinged so each finish notifies
exactly once. Kept separate from the wake *policy* (`wake.py`) — wake decides
whether to fire an autonomous turn, this decides whether to ping the desktop;
they never share state. Free of Textual and the notifier daemon so the dedup
decision is unit-testable on its own."""

from __future__ import annotations

from typing import TYPE_CHECKING, Iterable

if TYPE_CHECKING:
    from marim_harness.jobs import Job


class FinishedJobNotifier:
    """Owns the set of job ids already desktop-notified. The App turns the
    returned list into actual notifications (the off-event-loop ``send``)."""

    def __init__(self) -> None:
        self.notified: set[str] = set()

    def newly_finished(self, jobs: Iterable[Job]) -> list[Job]:
        """Return each job that has genuinely completed (``done``/``failed``) and
        has not been notified yet, recording its id so a later poll skips it.
        Cancelled jobs are excluded — they're agent-initiated or shutdown
        teardown, so a ping would be noise (and ``cancel_all`` on exit stays
        silent)."""
        fresh: list[Job] = []
        for job in jobs:
            if job.status in ("done", "failed") and job.id not in self.notified:
                self.notified.add(job.id)
                fresh.append(job)
        return fresh
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_finished_job_notifier.py -v`
Expected: PASS (all 4).

- [ ] **Step 5: Rewire `HarnessApp.__init__`**

In `app.py`, replace (line 135):

```python
        self._notified_jobs: set[str] = set()
```

with:

```python
        self._job_notifier = FinishedJobNotifier()
```

Add the import near the other tui-local imports: `from .notify import FinishedJobNotifier`.

- [ ] **Step 6: Shrink `_notify_finished_jobs`**

Replace the body (currently lines 314-327) with:

```python
    def _notify_finished_jobs(self) -> None:
        """Desktop-notify once per genuinely completed (done/failed) background
        job. Decoupled from the autonomous-wake path so a completion still pings
        when wake is off, a turn is busy, or the depth cap is hit. Cancelled jobs
        are skipped — they're either agent-initiated or shutdown teardown, so a
        ping would be noise (and this keeps ``cancel_all`` on exit silent)."""
        for job in self._job_notifier.newly_finished(self.harness.deps.jobs.list()):
            self._notify(
                "Background job finished",
                f"{job.id} ({job.kind}) {job.status}",
                "job_done",
            )
```

- [ ] **Step 7: Verify no stale references remain**

Run: `grep -n "_notified_jobs" src/marim_harness/interfaces/tui/app.py`
Expected: no output.

- [ ] **Step 8: Run the gate**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: PASS.

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/interfaces/tui/notify.py src/marim_harness/interfaces/tui/app.py tests/test_finished_job_notifier.py
git commit -m "refactor(tui): extract FinishedJobNotifier from HarnessApp

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013z5J2JSck7fQBLSyP9B4Eb"
```

---

### Task 3: `spend_tag` + `SubAgentViewer` — extract the viewer cursor + spend formatter

**Files:**
- Create: `src/marim_harness/interfaces/tui/subagent_view.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (viewer actions + `_apply_subagent_view`; remove `_subagent_spend`)
- Test: `tests/test_subagent_view.py` (new)

**Interfaces:**
- Consumes: `human_tokens(n: int) -> str` from `marim_harness.interfaces.tui.widgets.format`.
- Produces:
  - `spend_tag(tokens: int, max_ctx: int) -> str` — `""` when `tokens` falsy; `human_tokens(tokens)` when `max_ctx` is 0/falsy; `f"{human_tokens(tokens)} ({pct}%)"` otherwise, `pct = round(tokens / max_ctx * 100)`.
  - `SubAgentViewer()` — no args. `.open: bool` (default False), `.index: int` (default 0), `.clamp(count: int) -> int` (sets+returns `max(0, min(index, count-1))`), `.prev() -> None` (`index -= 1`), `.next() -> None` (`index += 1`).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_subagent_view.py`:

```python
from marim_harness.interfaces.tui.subagent_view import SubAgentViewer, spend_tag


def test_spend_tag_empty_when_no_tokens():
    assert spend_tag(0, 100_000) == ""


def test_spend_tag_tokens_only_when_no_max_ctx():
    # human_tokens(1500) -> "1.5k"; no percentage without a context size.
    assert spend_tag(1500, 0) == "1.5k"


def test_spend_tag_includes_percentage_when_max_ctx_known():
    # 1500 / 150000 = 1% share.
    assert spend_tag(1500, 150_000) == "1.5k (1%)"


def test_viewer_defaults():
    v = SubAgentViewer()
    assert v.open is False
    assert v.index == 0


def test_clamp_pins_into_range_and_returns_index():
    v = SubAgentViewer()
    v.index = 9
    assert v.clamp(3) == 2  # last valid index for 3 items
    assert v.index == 2


def test_clamp_floors_at_zero():
    v = SubAgentViewer()
    v.index = -5
    assert v.clamp(4) == 0
    assert v.index == 0


def test_prev_next_step_the_cursor():
    v = SubAgentViewer()
    v.next()
    v.next()
    assert v.index == 2
    v.prev()
    assert v.index == 1
```

(If `human_tokens(1500)` does not format as `"1.5k"`, adjust the two literal expectations in steps to match its real output — run `uv run python -c "from marim_harness.interfaces.tui.widgets.format import human_tokens; print(human_tokens(1500))"` to confirm before finalizing.)

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest --no-cov tests/test_subagent_view.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'marim_harness.interfaces.tui.subagent_view'`.

- [ ] **Step 3: Implement `subagent_view.py`**

Create `src/marim_harness/interfaces/tui/subagent_view.py`:

```python
"""Cursor + spend formatting for the full-screen sub-agent viewer (ctrl+x).

Holds only the open/index state and the pure spend-tag formatter; the App owns
every widget effect (mount, display toggling, focus, ``stream.viewing_sid``).
Free of Textual so the clamp/step arithmetic and the tag formatting are
unit-testable without an App."""

from __future__ import annotations

from .widgets.format import human_tokens


def spend_tag(tokens: int, max_ctx: int) -> str:
    """A compact ``{tokens} ({pct}%)`` spend tag for the footer, where pct is the
    share of the model's context window. Empty until the spawn is metered; drops
    the percentage when the context size is unknown."""
    if not tokens:
        return ""
    tag = human_tokens(tokens)
    if max_ctx:
        tag += f" ({round(tokens / max_ctx * 100)}%)"
    return tag


class SubAgentViewer:
    """The viewer's cursor: whether it's open and which spawn is selected. The
    App reads/sets these and performs all the widget effects around them."""

    def __init__(self) -> None:
        self.open = False
        self.index = 0

    def clamp(self, count: int) -> int:
        """Pin the index into ``[0, count-1]`` and return it."""
        self.index = max(0, min(self.index, count - 1))
        return self.index

    def prev(self) -> None:
        self.index -= 1

    def next(self) -> None:
        self.index += 1
```

- [ ] **Step 4: Run the new tests to verify they pass**

Run: `uv run pytest --no-cov tests/test_subagent_view.py -v`
Expected: PASS (all 7).

- [ ] **Step 5: Rewire `HarnessApp.__init__`**

In `app.py`, replace (lines 139-140):

```python
        self.subagent_viewer_open = False
        self.subagent_index = 0
```

with:

```python
        self._viewer = SubAgentViewer()
```

Add the import: `from .subagent_view import SubAgentViewer, spend_tag`.

- [ ] **Step 6: Rewire the viewer action methods**

`action_toggle_subagents` (372-377) — line 374 `if self.subagent_viewer_open:` → `if self._viewer.open:`

`action_subagent_prev` (383-386):

```python
    def action_subagent_prev(self) -> None:
        if self._viewer.open:
            self._viewer.prev()
            self._apply_subagent_view()
```

`action_subagent_next` (388-391):

```python
    def action_subagent_next(self) -> None:
        if self._viewer.open:
            self._viewer.next()
            self._apply_subagent_view()
```

`_open_subagents` (393-406) — replace lines 400 and 402:

```python
        self._viewer.open = True
        # Open on the most recent spawn (the one you most likely just watched).
        self._viewer.index = len(subs) - 1
```

`_close_subagents` (408-419) — line 409 `self.subagent_viewer_open = False` → `self._viewer.open = False`

- [ ] **Step 7: Rewire `_apply_subagent_view` and delete `_subagent_spend`**

Replace `_apply_subagent_view` (421-447) with (the clamp now goes through the viewer; `_subagent_spend(current)` becomes `spend_tag(...)`):

```python
    def _apply_subagent_view(self) -> None:
        """Reveal the selected sub-agent's transcript in place and repaint the list
        and footer. Clamps the index and closes the viewer if the list is empty."""
        subs = self.stream.subagents
        if not subs:
            self._close_subagents()
            return
        idx = self._viewer.clamp(len(subs))
        current = subs[idx]
        # Exactly one transcript carries the overlay (`viewing`) class + display at a
        # time; the rest stay hidden inline. Never reparented — just toggled.
        for i, w in enumerate(subs):
            if i == idx:
                w.body.add_class("viewing")
                w.body.display = True
            else:
                w.body.remove_class("viewing")
                w.body.display = False
        self.stream.viewing_sid = current.stream_id
        self.query_one(SubAgentList).show_subagents(subs, idx)
        max_ctx = getattr(self.harness.session, "max_context_tokens", 0) or 0
        self.query_one("#subagent-footer", SubAgentFooter).show_status(
            current.agent_type, idx, len(subs),
            spend_tag(current.tokens, max_ctx),
        )
        # Render the just-revealed transcript now rather than waiting for the next
        # flush tick (its streams were skipped while it wasn't being viewed).
        self.stream.flush_streams()
```

Then delete the entire `_subagent_spend` method (449-458) — `spend_tag` replaces it.

- [ ] **Step 8: Verify no stale references remain**

Run: `grep -nE "subagent_viewer_open|subagent_index|_subagent_spend" src/marim_harness/interfaces/tui/app.py`
Expected: no output.

- [ ] **Step 9: Run the gate**

Run: `uv run ruff check src tests && uv run pyright && uv run pytest`
Expected: PASS. Existing sub-agent tests stay green.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/interfaces/tui/subagent_view.py src/marim_harness/interfaces/tui/app.py tests/test_subagent_view.py
git commit -m "refactor(tui): extract SubAgentViewer + spend_tag from HarnessApp

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_013z5J2JSck7fQBLSyP9B4Eb"
```

---

## Self-Review

**Spec coverage:**
- `TurnQueue` (spec §Components.1) → Task 1. All six methods (`enqueue`, `prepend`, `pop_next`, `remove`, `take`, `items`/`__bool__`) + `paused` covered; the front-insert subtlety has a dedicated test.
- `FinishedJobNotifier` (spec §Components.2) → Task 2. `newly_finished` dedup + cancelled-exclusion covered.
- `spend_tag` + `SubAgentViewer` (spec §Components.3) → Task 3. Three spend branches + clamp/step covered.
- "NOT changing" (spec) — no task touches autocomplete/input routing/turn driving. ✓
- Testing (spec §Testing) — every listed unit test appears. Regression net (existing tests untouched) is the gate step in each task. ✓
- Sequencing (spec §Sequencing) — Task order matches: queue → notifier → viewer, each its own commit gated by ruff→pyright→pytest. ✓

**Placeholder scan:** No TBD/TODO/"handle edge cases"/"similar to". Every code step shows full code. The one conditional ("if `human_tokens(1500)` doesn't format as `1.5k`") includes the exact command to confirm and what to adjust — not a placeholder, a guard.

**Type consistency:** `TurnQueue` method names identical across Interfaces block, implementation, and App rewiring (`enqueue`/`prepend`/`pop_next`/`remove`/`take`/`items`/`paused`). `FinishedJobNotifier.newly_finished` consistent. `SubAgentViewer.clamp/prev/next/open/index` and `spend_tag(tokens, max_ctx)` consistent across the test, impl, and `_apply_subagent_view` rewiring.

**Line-number caveat:** All `app.py` line numbers are from the spec-time snapshot. Tasks 2 and 3 run after Task 1 has already shifted lines — the implementer should locate by method name / surrounding code, not trust the absolute numbers. Each task's stale-reference grep (Step 8) is the real safety net.
