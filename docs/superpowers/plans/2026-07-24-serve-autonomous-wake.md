# Serve-mode Autonomous Wake Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Make a finished background job fire a digest-only autonomous turn under `marim serve` (today it only happens in the interactive TUI), via a shared `WakeDriver` both interfaces consume, and mark autonomous turns on the wire.

**Architecture:** Move the Textual-free `WakeController` policy to `runtime/`, add a `runtime/wake_driver.py` orchestrator that runs the policy and enqueues a digest turn through an injected callback, wire it into `SessionHost` (two triggers: job-settle and turn-end) and migrate the TUI onto it. `turn.started` gains an additive `trigger` field.

**Tech Stack:** Python 3.12, asyncio, Starlette/uvicorn server, pytest. Package manager: `uv`.

## Global Constraints

- Package manager is **`uv`** — never bare `python`/`pytest`. Full gate: `uv run ruff check` then `uv run pytest -q` (the latter enforces `--cov-fail-under=90`). Targeted runs during TDD use `uv run pytest <path> --no-cov`.
- `ruff check` must be clean; follow the repo's existing import style and line length (match surrounding files).
- The `turn.started` `trigger` field is **additive** — existing consumers (and the mobile client's `ignoreUnknownKeys`) must keep working; never remove or rename existing event fields.
- The TUI migration is **behavior-preserving**: `tests/test_wake.py` and any app-level wake tests must stay green with no assertion changes beyond the import path.
- `WakeController` policy semantics are unchanged (depth cap, all-jobs-settled fan-out guard). Do not alter `should_wake`.
- Every commit message ends with:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx
  ```

---

### Task 1: Relocate `WakeController` to shared runtime

**Files:**
- Move: `src/marim_harness/interfaces/tui/wake.py` → `src/marim_harness/runtime/wake.py`
- Modify: `src/marim_harness/interfaces/tui/app.py:44` (import path)
- Modify: `tests/test_wake.py:3` (import path)

**Interfaces:**
- Produces: `marim_harness.runtime.wake.WakeController` — unchanged class (`should_wake(...)`, `record_auto_turn()`, `reset()`, `depth`, `depth_cap`). This is the single home both `WakeDriver` and the TUI import from after this task.

- [ ] **Step 1: Move the file with git**

Run: `git mv src/marim_harness/interfaces/tui/wake.py src/marim_harness/runtime/wake.py`

- [ ] **Step 2: Update the module docstring**

The class is no longer TUI-specific. Change the opening line of `src/marim_harness/runtime/wake.py` from:

```python
"""Autonomous wake-on-completion policy for the interactive TUI.
```

to:

```python
"""Autonomous wake-on-completion policy, shared by the interactive TUI and the
serve-mode SessionHost.
```

Leave the rest of the docstring and the class body unchanged.

- [ ] **Step 3: Fix the TUI import**

In `src/marim_harness/interfaces/tui/app.py:44`, change:

```python
from .wake import WakeController
```

to:

```python
from ...runtime.wake import WakeController
```

- [ ] **Step 4: Fix the test import**

In `tests/test_wake.py:3`, change:

```python
from marim_harness.interfaces.tui.wake import WakeController
```

to:

```python
from marim_harness.runtime.wake import WakeController
```

- [ ] **Step 5: Confirm nothing else imports the old path**

Run: `grep -rn "interfaces.tui.wake\|from .wake import\|from \.\.wake" src tests`
Expected: no matches.

- [ ] **Step 6: Run the wake tests + lint**

Run: `uv run pytest tests/test_wake.py --no-cov -q && uv run ruff check src/marim_harness/runtime/wake.py src/marim_harness/interfaces/tui/app.py`
Expected: all pass, ruff clean.

- [ ] **Step 7: Commit**

```bash
git add -A
git commit -m "refactor(wake): move WakeController policy to runtime/ for sharing

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx"
```

---

### Task 2: `WakeDriver` orchestrator + unit tests

**Files:**
- Create: `src/marim_harness/runtime/wake_driver.py`
- Test: `tests/test_wake_driver.py`

**Interfaces:**
- Consumes: `marim_harness.runtime.wake.WakeController` (Task 1).
- Produces: `WakeDriver(controller, *, is_enabled, turn_busy, has_finished_pending, all_jobs_settled, enqueue_digest_turn)` with `maybe_wake() -> bool` and `note_user_turn() -> None`. Consumed by `SessionHost` (Task 3) and the TUI (Task 4).

- [ ] **Step 1: Write the failing tests**

Create `tests/test_wake_driver.py`:

```python
"""Unit tests for the shared WakeDriver orchestrator. Pure — the predicates and
the enqueue effect are injected, so no server and no Textual are involved."""

from marim_harness.runtime.wake import WakeController
from marim_harness.runtime.wake_driver import WakeDriver


def _driver(**over):
    """A driver whose predicates default to 'ready to wake' and whose enqueue
    appends to a list, so a test can flip one predicate and assert the effect."""
    fired: list[int] = []
    cfg = dict(
        is_enabled=lambda: True,
        turn_busy=lambda: False,
        has_finished_pending=lambda: True,
        all_jobs_settled=lambda: True,
    )
    cfg.update(over)
    driver = WakeDriver(
        WakeController(depth_cap=3),
        enqueue_digest_turn=lambda: fired.append(1),
        **cfg,
    )
    return driver, fired


def test_maybe_wake_fires_enqueue_when_ready():
    driver, fired = _driver()
    assert driver.maybe_wake() is True
    assert fired == [1]


def test_maybe_wake_suppressed_when_disabled():
    driver, fired = _driver(is_enabled=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_when_turn_busy():
    driver, fired = _driver(turn_busy=lambda: True)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_when_a_job_still_running():
    driver, fired = _driver(all_jobs_settled=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_maybe_wake_suppressed_without_pending_digest():
    driver, fired = _driver(has_finished_pending=lambda: False)
    assert driver.maybe_wake() is False
    assert fired == []


def test_depth_cap_bounds_the_chain():
    driver, fired = _driver()
    assert driver.maybe_wake() is True   # depth 0 -> 1
    assert driver.maybe_wake() is True   # depth 1 -> 2
    assert driver.maybe_wake() is True   # depth 2 -> 3
    assert driver.maybe_wake() is False  # depth 3 == cap -> capped
    assert fired == [1, 1, 1]


def test_note_user_turn_resets_the_chain():
    driver, fired = _driver()
    for _ in range(3):
        driver.maybe_wake()
    assert driver.maybe_wake() is False  # at cap
    driver.note_user_turn()
    assert driver.maybe_wake() is True   # chain reset -> wakes again
```

- [ ] **Step 2: Run to verify it fails**

Run: `uv run pytest tests/test_wake_driver.py --no-cov -q`
Expected: FAIL — `ModuleNotFoundError: marim_harness.runtime.wake_driver`.

- [ ] **Step 3: Implement `WakeDriver`**

Create `src/marim_harness/runtime/wake_driver.py`:

```python
"""Autonomous-wake orchestration shared by the interactive TUI and the serve-mode
SessionHost.

`WakeController` (``runtime/wake.py``) owns the *decision* — whether to wake now
and the depth counter that bounds runaway wake -> spawn -> wake chains. This
object owns the *effect* both interfaces would otherwise duplicate: on a
job-settle or turn-end signal, run the policy and, if it says wake, count the
turn and enqueue exactly one digest-only turn through the injected callback.

Kept free of Textual and of the server so the orchestration is unit-testable with
plain predicates. Each consumer injects its own notion of "a turn is in flight"
(``turn_busy``), the job-registry predicates, the runtime-toggled ``is_enabled``
flag, and how to actually enqueue a digest turn (``enqueue_digest_turn``).

Two trigger points, both required, both routed through :meth:`maybe_wake`:
job-settle (a job finished) and turn-end (a digest that arrived while a turn was
busy must still wake once that turn drains). Call :meth:`note_user_turn` on every
user-initiated turn to reset the depth chain.
"""

from __future__ import annotations

from collections.abc import Callable

from .wake import WakeController


class WakeDriver:
    def __init__(
        self,
        controller: WakeController,
        *,
        is_enabled: Callable[[], bool],
        turn_busy: Callable[[], bool],
        has_finished_pending: Callable[[], bool],
        all_jobs_settled: Callable[[], bool],
        enqueue_digest_turn: Callable[[], None],
    ) -> None:
        self._controller = controller
        self._is_enabled = is_enabled
        self._turn_busy = turn_busy
        self._has_finished_pending = has_finished_pending
        self._all_jobs_settled = all_jobs_settled
        self._enqueue_digest_turn = enqueue_digest_turn

    def maybe_wake(self) -> bool:
        """Run the wake policy for the current signal; if it fires, count the turn
        and enqueue one digest-only turn. Returns whether it enqueued. Safe to call
        on both the job-settle and turn-end triggers — the policy's guards make a
        redundant call a no-op."""
        if not self._controller.should_wake(
            enabled=self._is_enabled(),
            turn_busy=self._turn_busy(),
            has_finished_pending=self._has_finished_pending(),
            all_jobs_settled=self._all_jobs_settled(),
        ):
            return False
        self._controller.record_auto_turn()
        self._enqueue_digest_turn()
        return True

    def note_user_turn(self) -> None:
        """Reset the depth chain — call when a user-initiated turn is submitted."""
        self._controller.reset()
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_wake_driver.py --no-cov -q`
Expected: PASS (7 tests).

- [ ] **Step 5: Lint**

Run: `uv run ruff check src/marim_harness/runtime/wake_driver.py tests/test_wake_driver.py`
Expected: clean.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/wake_driver.py tests/test_wake_driver.py
git commit -m "feat(wake): shared WakeDriver orchestrator + unit tests

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx"
```

---

### Task 3: Wire `WakeDriver` into `SessionHost` (the serve-mode fix)

**Files:**
- Modify: `src/marim_harness/server/host.py`
- Test: `tests/test_server_host.py`

**Interfaces:**
- Consumes: `WakeController` (Task 1), `WakeDriver` (Task 2), and the existing `JobRegistry` predicates `has_finished_pending()` / `any_running()`, plus `harness.autonomous_wake` / `harness.wake_depth_cap`.
- Produces: `turn.started` events now carry `"trigger": "user" | "autonomous"`. A settled job on an idle host enqueues an autonomous digest turn.

**Note on imports:** match the file's existing style for referencing `runtime` (relative `..runtime...` vs absolute `marim_harness.runtime...`). Both `WakeController` and `WakeDriver` come from `marim_harness.runtime`.

- [ ] **Step 1: Write the failing tests**

Append to `tests/test_server_host.py` (helpers `_make_deps`, `_make_harness`, `_text_only_model`, `_spy`, `_drain_until`, `_wait_for`, and `SessionHost`/`EventBus`/`Mode` imports already exist in this file):

```python
async def _settling_job(host, *, label="explore: probe", result="job result"):
    """Register a background job on the host that finishes immediately, so its
    settle drives the on_jobs_changed -> maybe_wake path."""
    async def work():
        return result
    return host.harness.deps.jobs.register("agent", label, work())


async def test_settled_job_wakes_idle_session_with_autonomous_trigger(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    events = _spy(host.bus)
    await _settling_job(host)
    started = await _drain_until(events, "turn.started")
    assert started.data["trigger"] == "autonomous"
    assert started.data["prompt"] == ""
    await _drain_until(events, "turn.finished")
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()


async def test_user_turn_carries_user_trigger(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    host = SessionHost(_make_harness(_text_only_model(), deps), EventBus())
    events = _spy(host.bus)
    host.submit("hi")
    started = await _drain_until(events, "turn.started")
    assert started.data["trigger"] == "user"
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()


async def test_job_settled_mid_turn_wakes_after_turn_ends(tmp_path):
    deps = _make_deps(tmp_path, mode=Mode.auto)
    release = asyncio.Event()

    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="done")])

    async def stream_fn(messages, info):
        await release.wait()
        yield "done"

    host = SessionHost(_make_harness(FunctionModel(fn, stream_function=stream_fn), deps),
                       EventBus())
    events = _spy(host.bus)
    host.submit("do work")                       # user turn starts, blocks
    await _drain_until(events, "turn.started")    # (the user turn)
    await _settling_job(host)                     # settles WHILE the turn is busy
    await asyncio.sleep(0.05)
    assert [e for e in events
            if e.type == "turn.started" and e.data.get("trigger") == "autonomous"] == []
    release.set()                                 # let the user turn finish
    await _wait_for(lambda: any(
        e.type == "turn.started" and e.data.get("trigger") == "autonomous"
        for e in events))
    await _wait_for(lambda: host.status == "idle")
    await host.aclose()
```

- [ ] **Step 2: Run to verify they fail**

Run: `uv run pytest tests/test_server_host.py --no-cov -q -k "trigger or mid_turn"`
Expected: FAIL — `turn.started` has no `trigger` key / no autonomous turn is enqueued.

- [ ] **Step 3: Extend the queue tuple type + construct the driver**

In `src/marim_harness/server/host.py`, add the imports (matching the file's style), change the queue annotation, and construct the driver in `__init__` **before** the `harness.bind_ui(...)` call. Replace:

```python
        self._queue: asyncio.Queue[tuple[str, str, list | None]] = asyncio.Queue(
            maxsize=queue_limit
        )
        self._pending: dict[str, PendingAsk] = {}
        self._turn_task: asyncio.Task | None = None
        self._closing = False
        loop = asyncio.get_running_loop()
        self._idle_since = loop.time()
        harness.bind_ui(
```

with:

```python
        self._queue: asyncio.Queue[tuple[str, str, list | None, str]] = asyncio.Queue(
            maxsize=queue_limit
        )
        self._pending: dict[str, PendingAsk] = {}
        self._turn_task: asyncio.Task | None = None
        self._closing = False
        loop = asyncio.get_running_loop()
        self._idle_since = loop.time()
        jobs = harness.deps.jobs
        self._wake = WakeDriver(
            WakeController(harness.wake_depth_cap),
            is_enabled=lambda: harness.autonomous_wake,
            # "a turn is in flight" — NOT status == "running": a turn parked on an
            # ask reports "waiting_ask" while its task is still live, and a wake
            # turn must not queue behind it.
            turn_busy=lambda: self.status != "idle",
            has_finished_pending=jobs.has_finished_pending,
            all_jobs_settled=lambda: not jobs.any_running(),
            enqueue_digest_turn=self._enqueue_autonomous_turn,
        )
        harness.bind_ui(
```

Add the imports near the other `marim_harness.runtime` imports:

```python
from marim_harness.runtime.wake import WakeController
from marim_harness.runtime.wake_driver import WakeDriver
```

(or the relative form if the file uses relative imports — match the surrounding lines).

- [ ] **Step 4: Route `on_jobs_changed` through a method that also wakes**

In the `harness.bind_ui(...)` call, change:

```python
            on_jobs_changed=lambda: self._publish("jobs.changed", {}),
```

to:

```python
            on_jobs_changed=self._on_jobs_changed,
```

Then add the method (near the other `bind_ui` bridge callbacks, e.g. just above `_publish_status`):

```python
    def _on_jobs_changed(self) -> None:
        """A job launched or settled. Poke the jobs view, then let the wake driver
        decide whether a completion warrants an autonomous digest turn (trigger 1
        of 2 — the other is the turn-end check in _worker_loop)."""
        self._publish("jobs.changed", {})
        self._wake.maybe_wake()
```

- [ ] **Step 5: Add the autonomous-turn enqueue + reset the chain on user submit**

Add the enqueue method (near `submit`):

```python
    def _enqueue_autonomous_turn(self) -> None:
        """Queue one digest-only turn (empty prompt) marked autonomous. Best-effort:
        a full queue drops the wake rather than raising into a job callback — the
        pending digest survives and a later trigger can still fire it."""
        turn_id = secrets.token_hex(8)
        try:
            self._queue.put_nowait((turn_id, "", None, "autonomous"))
        except asyncio.QueueFull:
            pass
```

Change `submit` from:

```python
    def submit(self, prompt: str, attachments: list | None = None) -> str:
        if self._closing:
            raise HostClosed()
        turn_id = secrets.token_hex(8)
        try:
            self._queue.put_nowait((turn_id, prompt, attachments))
        except asyncio.QueueFull:
            raise TurnQueueFull() from None
        return turn_id
```

to:

```python
    def submit(self, prompt: str, attachments: list | None = None) -> str:
        if self._closing:
            raise HostClosed()
        turn_id = secrets.token_hex(8)
        self._wake.note_user_turn()  # a user turn resets the autonomous-wake chain
        try:
            self._queue.put_nowait((turn_id, prompt, attachments, "user"))
        except asyncio.QueueFull:
            raise TurnQueueFull() from None
        return turn_id
```

- [ ] **Step 6: Unpack the 4-tuple, thread `trigger`, add the turn-end trigger**

Change `_worker_loop` from:

```python
    async def _worker_loop(self) -> None:
        while True:
            turn_id, prompt, attachments = await self._queue.get()
            self._turn_task = asyncio.get_running_loop().create_task(
                self._run_one_turn(turn_id, prompt, attachments)
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._closing:
                    raise
                self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
            finally:
                self._turn_task = None
                self._cancel_pending("interrupted")
                self._idle_since = asyncio.get_running_loop().time()
                self._publish_status()
```

to:

```python
    async def _worker_loop(self) -> None:
        while True:
            turn_id, prompt, attachments, trigger = await self._queue.get()
            self._turn_task = asyncio.get_running_loop().create_task(
                self._run_one_turn(turn_id, prompt, attachments, trigger)
            )
            try:
                await self._turn_task
            except asyncio.CancelledError:
                if self._closing:
                    raise
                self.bus.publish("turn.finished", {"turn_id": turn_id, "interrupted": True})
            finally:
                self._turn_task = None
                self._cancel_pending("interrupted")
                self._idle_since = asyncio.get_running_loop().time()
                self._publish_status()
                # Trigger 2: a job that settled while this turn was busy left a
                # pending digest the settle-time check had to skip. Re-check now
                # that the worker is idle. Guard on _closing so teardown never
                # enqueues a turn into a worker being cancelled.
                if not self._closing:
                    self._wake.maybe_wake()
```

- [ ] **Step 7: Emit the `trigger` marker on `turn.started`**

Change `_run_one_turn`'s signature and first publish from:

```python
    async def _run_one_turn(self, turn_id: str, prompt: str, attachments) -> None:
        self.bus.publish("turn.started", {"turn_id": turn_id, "prompt": prompt})
```

to:

```python
    async def _run_one_turn(
        self, turn_id: str, prompt: str, attachments, trigger: str = "user"
    ) -> None:
        self.bus.publish(
            "turn.started", {"turn_id": turn_id, "prompt": prompt, "trigger": trigger}
        )
```

- [ ] **Step 8: Run the new tests + the full host suite**

Run: `uv run pytest tests/test_server_host.py --no-cov -q`
Expected: PASS (existing tests + the 3 new ones).

- [ ] **Step 9: Lint**

Run: `uv run ruff check src/marim_harness/server/host.py tests/test_server_host.py`
Expected: clean.

- [ ] **Step 10: Commit**

```bash
git add src/marim_harness/server/host.py tests/test_server_host.py
git commit -m "feat(wake): serve-mode autonomous wake via WakeDriver + turn trigger marker

SessionHost now fires a digest-only autonomous turn when a background job
settles on an idle session (job-settle and turn-end triggers), and stamps
turn.started with trigger=user|autonomous.

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx"
```

---

### Task 4: Migrate the TUI onto the shared `WakeDriver`

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`

**Interfaces:**
- Consumes: `WakeController` (Task 1, already imported), `WakeDriver` (Task 2).
- Produces: no behavior change. `tests/test_wake.py` and any app-level wake tests remain the guard.

- [ ] **Step 1: Check for app-level references to `_wake` internals**

Run: `grep -rn "\._wake\b" src/marim_harness/interfaces/tui tests`
Note every hit. After migration `self._wake` is a `WakeDriver`, so any test or code calling `self._wake.should_wake(...)` / `.depth` / `.reset()` directly must move to `WakeController` (unit-tested in `test_wake.py`) or to `.note_user_turn()`. The app itself uses only `_maybe_wake()` and (at ~app.py:525) `self._wake.reset()`.

- [ ] **Step 2: Add the `WakeDriver` import**

In `src/marim_harness/interfaces/tui/app.py`, next to the existing `from ...runtime.wake import WakeController` (line 44 after Task 1), add:

```python
from ...runtime.wake_driver import WakeDriver
```

- [ ] **Step 3: Construct a `WakeDriver` instead of a bare controller**

Change (app.py ~176):

```python
        self._wake = WakeController(harness.wake_depth_cap)
```

to:

```python
        self._wake = WakeDriver(
            WakeController(harness.wake_depth_cap),
            is_enabled=lambda: self.autonomous_wake,
            turn_busy=lambda: self.turn_busy,
            has_finished_pending=self.jobs.has_finished_pending,
            all_jobs_settled=lambda: not self.jobs.any_running(),
            enqueue_digest_turn=self._mount_wake_turn,
        )
```

(`self.autonomous_wake` is set just above at ~app.py:173; `self.jobs` and `self.turn_busy` are properties.)

- [ ] **Step 4: Replace the `_maybe_wake` body and extract the mount effect**

Replace the existing `_maybe_wake` method (the `should_wake` guard block at ~app.py:422-444, ending at the `self._wake.record_auto_turn()` + notice + `run_worker` lines) with:

```python
    def _maybe_wake(self) -> None:
        """Fire one digest-only autonomous turn iff a background job finished and
        nothing is blocking. The decision + depth bookkeeping live in the shared
        WakeDriver; this method only supplies the is-running mount guard."""
        if not self.is_running:
            return  # firing during teardown would race the unmount
        self._wake.maybe_wake()

    def _mount_wake_turn(self) -> None:
        """The wake effect the driver invokes: post the resume notice and spawn the
        digest-only turn worker. Mounted synchronously (we may be in a sync
        on_change callback), mirroring _on_compact / _on_rename."""
        self._append_log(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)
```

Preserve the exact notice text and `run_worker(..., exclusive=True)` call from the original so behavior is unchanged.

- [ ] **Step 5: Swap the user-turn reset call**

At the user-turn reset site (~app.py:525), change:

```python
            self._wake.reset()
```

to:

```python
            self._wake.note_user_turn()
```

- [ ] **Step 6: Run the wake tests, TUI/app tests, and lint**

Run: `uv run pytest tests/test_wake.py --no-cov -q && uv run pytest -k "app or tui or wake" --no-cov -q && uv run ruff check src/marim_harness/interfaces/tui/app.py`
Expected: all pass, ruff clean. If an app test referenced `_wake` internals (Step 1), update it to target `WakeController`/`note_user_turn` — no assertion-intent change.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests
git commit -m "refactor(wake): migrate TUI onto shared WakeDriver (behavior-preserving)

Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01JiZ12obK27rwbg9iJwdZKx"
```

---

### Final verification (whole-branch, before finishing)

- [ ] **Full gate:** `uv run ruff check && uv run pytest -q`
  Expected: ruff clean; all tests pass with coverage ≥ 90%.
- [ ] Confirm the `trigger` field is the only wire-contract change and it is additive (no existing `turn.started` field removed/renamed).
- [ ] Confirm no import still points at `interfaces.tui.wake`.
