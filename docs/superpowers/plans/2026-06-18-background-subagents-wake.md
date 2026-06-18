# Background Subagents: Wake-on-Completion + Gap Fixes Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let a finished background sub-agent fire a turn on its own (autonomous wake-on-completion) in the interactive TUI, and harden the two remaining correctness/UX gaps in the existing background-subagent implementation.

**Architecture:** Background spawn, detached execution, poll tools, and the passive finished-digest already exist end-to-end. This plan adds (A) a wake scheduler in the TUI that fires a digest-only turn when a job finishes while the turn worker is idle, guarded by a depth cap + kill switch; (B) the autonomous turn entry (an ordinary `_run_turn("")`); (D) per-background-sub-agent `TaskList` isolation; and (E) a human-facing `/jobs` command. Component C (a usage-race lock) was dropped — the race it guarded cannot occur on the single-threaded asyncio loop (see the spec).

**Tech Stack:** Python 3.11+, asyncio, Textual (TUI), pydantic-ai, pytest (`anyio` asyncio backend), `uv` for env/deps.

## Global Constraints

- **Scope is the interactive TUI only.** Headless (`-p`) keeps today's behavior; do not touch the headless path.
- **Defaults:** depth cap = **3** (`wake_depth_cap`); autonomous wake **on** by default (`autonomous_wake = True`).
- **Config-flag pattern:** new config flags mirror the existing `job_tool_combined` flag exactly — a `ModelConfig` field with a default, parsed in `load_config` via `_bool_env`/`_int_env`, and threaded through **all three** provider return branches (local / google / openrouter).
- **No new turn engine.** The autonomous turn is an ordinary `_run_turn("")`; `_assemble_prompt("")` already prepends `take_finished_digest()`.
- **`has_finished_pending()` must not consume the digest** — only `take_finished_digest()` (called inside the turn) consumes it.
- **Single-threaded asyncio invariant:** do **not** add an `asyncio.Lock` around `session.usage`/`persist()` (Component C is out of scope; see spec).
- **Background sub-agents do not spawn further sub-agents**, and `Deps.jobs` stays shared — only `Deps.tasks` is isolated for background spawns.
- **TDD, frequent commits, DRY, YAGNI.** Each task ends with all gates green:
  `uv run ruff check src tests`, `uv run pyright src`, `uv run pytest`.
- Commit message trailers (every commit):
  ```
  Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x
  ```
- Branch is already `feat/background-subagents-wake` (non-default). Do not create a new branch. Stage only named files — never `git add -A`.

---

### Task 1: `JobRegistry.has_finished_pending()` predicate

**Files:**
- Modify: `src/marim_harness/jobs.py` (add a method to `JobRegistry`, near `take_finished_digest` at `jobs.py:199`)
- Test: `tests/test_jobs.py`

**Interfaces:**
- Consumes: the existing `JobRegistry._finished_since_turn: list[str]` (populated in `_settle`, drained by `take_finished_digest`).
- Produces: `JobRegistry.has_finished_pending() -> bool` — `True` iff one or more jobs finished since the last `take_finished_digest()`. Read-only; does **not** mutate `_finished_since_turn`. The wake scheduler (Task 4) calls this to decide whether to fire without consuming the digest.

- [ ] **Step 1: Write the failing test**

Add to `tests/test_jobs.py` (the `_sleep_then` helper at the bottom of the file is already in scope):

```python
@pytest.mark.anyio
async def test_has_finished_pending_reflects_set_without_consuming():
    reg = JobRegistry()
    # Nothing finished yet.
    assert reg.has_finished_pending() is False
    job_id = reg.register("agent", "a", _sleep_then("R", 0.01))
    assert reg.has_finished_pending() is False  # still running
    await reg.wait(job_id)
    # Finished -> pending, and checking it does NOT drain the digest.
    assert reg.has_finished_pending() is True
    assert reg.has_finished_pending() is True  # non-consuming
    digest = reg.take_finished_digest()
    assert "job-1 (agent) done" in digest  # the digest survived the peeks
    # Draining clears the pending flag.
    assert reg.has_finished_pending() is False
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_jobs.py::test_has_finished_pending_reflects_set_without_consuming -v`
Expected: FAIL with `AttributeError: 'JobRegistry' object has no attribute 'has_finished_pending'`

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/jobs.py`, add this method to `JobRegistry` immediately **above** `take_finished_digest` (around `jobs.py:199`):

```python
    def has_finished_pending(self) -> bool:
        """True if one or more jobs finished since the last
        :meth:`take_finished_digest`. Read-only — unlike ``take_finished_digest``
        it does **not** drain the buffer, so the wake scheduler can decide whether
        to fire an autonomous turn without consuming the digest the turn needs."""
        return bool(self._finished_since_turn)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_jobs.py::test_has_finished_pending_reflects_set_without_consuming -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/jobs.py tests/test_jobs.py
git commit -m "feat(jobs): add has_finished_pending() non-consuming predicate

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 2: Config flags `autonomous_wake` / `wake_depth_cap`

**Files:**
- Modify: `src/marim_harness/config/model.py` (`ModelConfig` fields + `load_config` parse + all three return branches)
- Modify: `src/marim_harness/agent.py` (`HarnessConfig` fields + `Harness.__init__` exposes them as attributes)
- Modify: `src/marim_harness/bootstrap.py` (thread the two flags into `HarnessConfig`)
- Test: `tests/test_config.py`, `tests/test_agent_subagents.py`

**Interfaces:**
- Consumes: the existing `_bool_env` / `_int_env` helpers in `config/model.py`.
- Produces:
  - `ModelConfig.autonomous_wake: bool` (default `True`) and `ModelConfig.wake_depth_cap: int` (default `3`), parsed from `MARIM_AUTONOMOUS_WAKE` / `MARIM_WAKE_DEPTH_CAP`.
  - `HarnessConfig.autonomous_wake: bool` (default `True`) and `HarnessConfig.wake_depth_cap: int` (default `3`).
  - `Harness.autonomous_wake: bool` and `Harness.wake_depth_cap: int` attributes — the TUI app (Task 4) reads these to initialize its scheduler state.

- [ ] **Step 1: Write the failing test (config parse)**

Add to `tests/test_config.py` (mirror the existing `test_job_tool_combined_*` tests near line 223; `load_config` is already imported there):

```python
def test_autonomous_wake_defaults_on(monkeypatch):
    monkeypatch.delenv("MARIM_AUTONOMOUS_WAKE", raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().autonomous_wake is True


@pytest.mark.parametrize("raw", ["0", "false", "off", "no"])
def test_autonomous_wake_falsy_disables(monkeypatch, raw):
    monkeypatch.setenv("MARIM_AUTONOMOUS_WAKE", raw)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().autonomous_wake is False


def test_wake_depth_cap_defaults_to_three(monkeypatch):
    monkeypatch.delenv("MARIM_WAKE_DEPTH_CAP", raising=False)
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().wake_depth_cap == 3


def test_wake_depth_cap_reads_env(monkeypatch):
    monkeypatch.setenv("MARIM_WAKE_DEPTH_CAP", "5")
    monkeypatch.setenv("MARIM_PROVIDER", "openrouter")
    assert load_config().wake_depth_cap == 5
```

Confirm `pytest` is imported at the top of `tests/test_config.py`; if not, add `import pytest`.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_config.py -k "autonomous_wake or wake_depth_cap" -v`
Expected: FAIL with `AttributeError: 'ModelConfig' object has no attribute 'autonomous_wake'`

- [ ] **Step 3: Write minimal implementation (config)**

In `src/marim_harness/config/model.py`, add two fields to `ModelConfig` immediately after the `job_tool_combined` field (line 33):

```python
    # Autonomous wake-on-completion (interactive TUI only): when a background job
    # finishes while the turn worker is idle, fire a digest-only turn so the agent
    # reacts without waiting for the user. Off ⇒ today's passive behavior.
    autonomous_wake: bool = True
    # Cap on consecutive autonomous turns before one is forced to wait for the
    # user — a loop guard for wake→spawn→wake chains.
    wake_depth_cap: int = 3
```

In `load_config`, add the parse next to `job_tool_combined = ...` (line 53):

```python
    autonomous_wake = _bool_env("MARIM_AUTONOMOUS_WAKE", True)
    wake_depth_cap = _int_env("MARIM_WAKE_DEPTH_CAP", 3)
```

Then add both to **each** of the three `ModelConfig(...)` return statements (local, google, openrouter), placed right after the `job_tool_combined=job_tool_combined,` line in each:

```python
            job_tool_combined=job_tool_combined,
            autonomous_wake=autonomous_wake,
            wake_depth_cap=wake_depth_cap,
```

(The openrouter branch uses 8-space indentation for its kwargs — match the surrounding lines in each branch.)

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_config.py -k "autonomous_wake or wake_depth_cap" -v`
Expected: PASS

- [ ] **Step 5: Write the failing test (Harness exposes the flags)**

Add to `tests/test_agent_subagents.py` (it already imports `Harness`, `Deps`, `Mode`, `BuiltinToolProvider`, and `_make_harness`/`_text_model` from `tests.conftest`):

```python
def test_harness_exposes_wake_defaults(tmp_path: Path):
    """The Harness surfaces the wake knobs so the TUI app can seed its scheduler;
    with no config passed, the defaults are on / cap 3."""
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = _make_harness(_text_model(), deps)
    assert h.autonomous_wake is True
    assert h.wake_depth_cap == 3


def test_harness_takes_wake_flags_from_config(tmp_path: Path):
    from marim_harness.agent import HarnessConfig

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    h = Harness(
        model=_text_model(), provider=BuiltinToolProvider(), deps=deps,
        instructions="x",
        config=HarnessConfig(autonomous_wake=False, wake_depth_cap=7),
    )
    assert h.autonomous_wake is False
    assert h.wake_depth_cap == 7
```

- [ ] **Step 6: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_subagents.py -k "wake" -v`
Expected: FAIL with `AttributeError: 'Harness' object has no attribute 'autonomous_wake'`

- [ ] **Step 7: Write minimal implementation (HarnessConfig + Harness + bootstrap)**

In `src/marim_harness/agent.py`, add two fields to `HarnessConfig` after `lsp_enabled` (line 209):

```python
    # Autonomous wake-on-completion knobs, surfaced to the TUI app. Defaults
    # match ModelConfig: wake on, cap 3.
    autonomous_wake: bool = True
    wake_depth_cap: int = 3
```

In `Harness.__init__`, after `self.model_id = cfg.model_id` (around `agent.py:251`), expose them:

```python
        # Surfaced for the TUI wake scheduler (interactive only).
        self.autonomous_wake = cfg.autonomous_wake
        self.wake_depth_cap = cfg.wake_depth_cap
```

In `src/marim_harness/bootstrap.py`, inside the `HarnessConfig(...)` passed to `Harness` (after `proactive_memory=cfg.proactive_memory,` at line 90):

```python
            proactive_memory=cfg.proactive_memory,
            autonomous_wake=cfg.autonomous_wake,
            wake_depth_cap=cfg.wake_depth_cap,
```

- [ ] **Step 8: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_subagents.py -k "wake" tests/test_config.py -k "autonomous_wake or wake_depth_cap" -v`
Expected: PASS (run them separately if the combined `-k` is awkward: `uv run pytest tests/test_agent_subagents.py::test_harness_exposes_wake_defaults tests/test_agent_subagents.py::test_harness_takes_wake_flags_from_config -v`)

- [ ] **Step 9: Commit**

```bash
git add src/marim_harness/config/model.py src/marim_harness/agent.py src/marim_harness/bootstrap.py tests/test_config.py tests/test_agent_subagents.py
git commit -m "feat(config): add autonomous_wake + wake_depth_cap, thread to Harness

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 3: Background sub-agent `TaskList` isolation (Component D)

**Files:**
- Modify: `src/marim_harness/subagents.py` (`run_background`, lines 128–153; imports at top)
- Test: `tests/test_agent_subagents.py`

**Interfaces:**
- Consumes: `dataclasses.replace`, `marim_harness.tasks.TaskList`, the existing `Deps` dataclass (`deps.py:36`, mutable `tasks: TaskList` field), and `SubagentRunner.build` / `self.deps` / `self.mcp` already used in `run_background`.
- Produces: `run_background` runs its sub-agent with a `Deps` whose `tasks` is a **fresh empty `TaskList`** (not the parent's). All other `Deps` fields (including `jobs`) are shared, via `replace(self.deps, tasks=TaskList())`. Foreground `run` is unchanged (still shares `self.deps`).

- [ ] **Step 1: Write the failing test**

Add to `tests/test_agent_subagents.py`. This captures the `deps` handed to the spawned agent's `run()` (the existing `_capture_subagent` helper only captures `toolsets`, so this test uses its own stub):

```python
@pytest.mark.anyio
async def test_run_background_isolates_task_list(tmp_path: Path):
    """A background sub-agent gets its OWN empty TaskList so its checklist never
    pollutes (or persists as) the user's session tasks. The foreground path keeps
    sharing the parent's tasks (it runs inside the turn, no race)."""
    from types import SimpleNamespace

    from pydantic_ai.usage import RunUsage

    from marim_harness.tasks import TaskList

    cap: dict = {}

    class _StubAgent:
        async def run(self, task, **kwargs):
            cap["deps"] = kwargs.get("deps")
            return SimpleNamespace(output="report", usage=RunUsage())

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    # Give the parent a non-empty checklist so a leak would be visible.
    deps.tasks.replace([{"text": "user task", "status": "in_progress"}])
    h = _make_harness(_text_model(), deps)
    h.subagents.build = lambda type, max_output_chars=None: (_StubAgent(), None)

    await h.subagents.run_background("explore", "scan")
    bg_deps = cap["deps"]
    # Background got a DIFFERENT, empty TaskList object...
    assert isinstance(bg_deps.tasks, TaskList)
    assert bg_deps.tasks is not deps.tasks
    assert bg_deps.tasks.as_list() == []
    # ...while jobs (and the rest of Deps) stay shared.
    assert bg_deps.jobs is deps.jobs
    # The parent's checklist is untouched.
    assert deps.tasks.as_list() == [{"text": "user task", "status": "in_progress"}]
```

> **Implementer note:** confirm `TaskList` exposes `replace(list)` and a read accessor returning a plain `list` of `{"text", "status"}` dicts. `replace` is used in `tests/test_app.py` (`app.harness.deps.tasks.replace([...])`). The read accessor used above is `as_list()` — if the real method has a different name (check `src/marim_harness/tasks.py`), use that name in both assertion lines. If `TaskList` has no plain-list accessor at all, assert emptiness via `len(bg_deps.tasks.as_dicts())` / the equivalent real method instead; do not add a new accessor just for the test.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_subagents.py::test_run_background_isolates_task_list -v`
Expected: FAIL on `assert bg_deps.tasks is not deps.tasks` (today `run_background` passes `deps=self.deps`, so it IS the same object)

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/subagents.py`, add the imports near the top (after line 12, `from typing import Optional`):

```python
from dataclasses import replace

from .tasks import TaskList
```

(`from .deps import Deps, SubAgent` is already imported at line 16, so `Deps` is a dataclass and `replace` works on it.)

In `run_background` (line 146), replace the single `sub.run` call line:

```python
        result = await sub.run(task, deps=self.deps, toolsets=granted)
```

with a fresh-tasks variant:

```python
        # A background sub-agent runs detached and concurrently with the user's
        # turn. Give it its own empty TaskList so its multi-step work never
        # mutates — or persists as — the user's session checklist. Every other
        # Deps field (jobs, workspace, hooks, lsp, …) stays shared.
        bg_deps = replace(self.deps, tasks=TaskList())
        result = await sub.run(task, deps=bg_deps, toolsets=granted)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_subagents.py::test_run_background_isolates_task_list -v`
Expected: PASS

- [ ] **Step 5: Run the full subagent suite (no regressions)**

Run: `uv run pytest tests/test_agent_subagents.py -v`
Expected: PASS — in particular `test_run_background_subagent_counts_and_persists_usage`, `test_run_background_subagent_grants_named_server`, and `test_run_background_subagent_respects_mode` still pass (isolation changed only `tasks`).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/subagents.py tests/test_agent_subagents.py
git commit -m "feat(subagents): give background sub-agents an isolated TaskList

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 4: Wake scheduler + autonomous turn (Components A + B)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (`HarnessApp.__init__`, `_on_jobs_changed`, `on_prompt_input_submitted`, `_run_turn`; add `_maybe_wake`)
- Test: `tests/test_app.py`

**Interfaces:**
- Consumes: `Harness.autonomous_wake` / `Harness.wake_depth_cap` (Task 2); `JobRegistry.has_finished_pending()` (Task 1); the existing `self._turn_worker`, `self.run_worker(..., exclusive=True)`, `self._run_turn`, `self.is_running`, and the `NoticeMessage` widget (already imported at `app.py:34`).
- Produces:
  - `HarnessApp.autonomous_wake: bool` (seeded from `harness.autonomous_wake`; flipped by `/jobs wake on|off` in Task 5).
  - `HarnessApp._auto_turn_depth: int` and `HarnessApp._wake_depth_cap: int`.
  - `HarnessApp._maybe_wake() -> None` — fires one digest-only autonomous turn iff wake is on, the turn worker is idle, depth `< cap`, and `jobs.has_finished_pending()`. Increments `_auto_turn_depth`, posts a `NoticeMessage`, and starts `self._run_turn("")` in the exclusive worker. A user-initiated turn resets `_auto_turn_depth = 0`.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_app.py` (the `_app`, `_submit`, and `_log_text` helpers already exist in this file). These stub `run_worker` so the autonomous turn is observed without running a real model turn:

```python
@pytest.mark.anyio
async def test_wake_fires_autonomous_turn_when_job_finishes_idle(tmp_path: Path):
    """A background job finishing while the turn worker is idle fires exactly one
    autonomous (empty-prompt) turn and arms the depth counter."""
    started: list = []

    def fake_worker(coro, *a, **k):
        started.append(coro)
        coro.close()  # don't actually run the turn
        return "worker"

    app = _app(tmp_path)
    app.run_worker = fake_worker  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        assert app.autonomous_wake is True  # seeded from harness default
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)  # completion fires on_change
        await pilot.pause()
        assert len(started) == 1  # one autonomous turn started
        assert app._auto_turn_depth == 1
        assert any("Resumed" in str(n.render()) for n in app.query(NoticeMessage))


@pytest.mark.anyio
async def test_wake_disabled_does_not_fire(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app.autonomous_wake = False
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []
        # The digest is left pending for the next user turn.
        assert app.harness.deps.jobs.has_finished_pending() is True


@pytest.mark.anyio
async def test_wake_stops_at_depth_cap(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._auto_turn_depth = app._wake_depth_cap  # already at the cap
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []  # capped, no further autonomous turn


@pytest.mark.anyio
async def test_wake_does_not_fire_while_a_turn_is_running(tmp_path: Path):
    started: list = []
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (started.append(c), c.close())  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._turn_worker = object()  # pretend a turn is in flight
        job_id = app.harness.deps.jobs.register("agent", "explore: x", _done("R"))
        await app.harness.deps.jobs.wait(job_id)
        await pilot.pause()
        assert started == []  # queued; drains on the next turn's completion
        app._turn_worker = None  # turn ends -> finally calls _maybe_wake
        app._maybe_wake()
        assert len(started) == 1


@pytest.mark.anyio
async def test_user_turn_resets_auto_depth(tmp_path: Path):
    app = _app(tmp_path)
    app.run_worker = lambda c, *a, **k: (c.close() if hasattr(c, "close") else None)  # type: ignore[method-assign]
    async with app.run_test() as pilot:
        await pilot.pause()
        app._auto_turn_depth = 2
        await _submit(app, "do something")  # a user-initiated turn
        assert app._auto_turn_depth == 0
```

Add the `_done` helper near the bottom of `tests/test_app.py` (next to the other module helpers):

```python
def _done(value: str):
    """A coroutine that resolves immediately to ``value`` — a finished job body."""
    async def coro():
        return value
    return coro()
```

Confirm `NoticeMessage` is importable in the test module — the existing
`test_compaction_shows_notice_in_log` imports it locally
(`from marim_harness.interfaces.tui.widgets import NoticeMessage`). Add the same
import at the top of `tests/test_app.py`, or import it locally inside each new
test that references it.

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_app.py -k "wake or auto_depth" -v`
Expected: FAIL — `AttributeError: 'HarnessApp' object has no attribute 'autonomous_wake'` (and `_maybe_wake` / `_wake_depth_cap` undefined).

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/interfaces/tui/app.py`, `HarnessApp.__init__`, after `self._turn_worker = None` (line 224) add the scheduler state:

```python
        # Autonomous wake-on-completion (interactive TUI only). When a background
        # job finishes while the turn worker is idle, fire a digest-only turn so
        # the agent reacts without waiting for the user. Seeded from config;
        # toggled at runtime by `/jobs wake on|off`.
        self.autonomous_wake = harness.autonomous_wake
        self._wake_depth_cap = harness.wake_depth_cap
        # Consecutive autonomous turns since the last user turn; reset on any
        # user-initiated turn. Bounds wake→spawn→wake chains.
        self._auto_turn_depth = 0
```

Change `_on_jobs_changed` (lines 423–427) to evaluate the scheduler after the repaint:

```python
    def _on_jobs_changed(self) -> None:
        """Live callback from the job registry — repaint as jobs launch and
        finish. Each job runs as a task on the app's event loop, so the callback
        fires there and direct widget mutation is safe."""
        self._render_jobs()
        self._maybe_wake()
```

Add the `_maybe_wake` helper immediately after `_on_jobs_changed`:

```python
    def _maybe_wake(self) -> None:
        """Fire one digest-only autonomous turn iff a background job has finished
        and nothing is blocking. Guards (all must hold): wake enabled, the turn
        worker is idle, the depth cap is not yet reached, and there is a pending
        finished-job digest. The digest itself is consumed later inside the turn
        by ``_assemble_prompt('')`` -> ``take_finished_digest()`` — this predicate
        only peeks, so a queued digest survives until a turn actually runs."""
        if not self.is_running:
            return  # firing during teardown would race the unmount
        if not self.autonomous_wake:
            return
        if self._turn_worker is not None:
            return  # a turn is running; the digest drains on the next turn
        if self._auto_turn_depth >= self._wake_depth_cap:
            return  # loop guard: wait for the user
        if not self.harness.deps.jobs.has_finished_pending():
            return  # nothing finished -> no empty turn
        self._auto_turn_depth += 1
        # Mounted synchronously (we may be in a sync on_change callback), mirroring
        # _on_compact / _on_rename.
        log = self.query_one("#log", VerticalScroll)
        log.mount(NoticeMessage("⏰ Resumed — background job(s) finished"))
        self._turn_worker = self.run_worker(self._run_turn(""), exclusive=True)
```

In `on_prompt_input_submitted`, reset the depth on a user turn. Change the
non-command tail (lines 592–595) to:

```python
        log = self.query_one("#log", VerticalScroll)
        await log.mount(UserMessage(text))
        self._current_assistant = None
        self._auto_turn_depth = 0  # a user turn breaks any autonomous-wake chain
        self._turn_worker = self.run_worker(self._run_turn(text), exclusive=True)
```

In `_run_turn`, drain a digest that arrived mid-turn once the worker clears.
Change the `finally` block (lines 609–611) to:

```python
        finally:
            self._turn_worker = None
            self._set_busy(False)
            self._maybe_wake()  # a job that finished mid-turn drains now
```

> **Implementer note:** `_maybe_wake` reads `self._turn_worker`, which `_run_turn`'s `finally` sets to `None` *before* the `_maybe_wake()` call above — so the idle guard sees the cleared worker. Because `_maybe_wake` re-enters `run_worker(self._run_turn(""))`, the depth cap is the only thing bounding a wake→finishes-again→wake chain; the cap guard (`_auto_turn_depth >= self._wake_depth_cap`) is what stops it. Do not also guard on the prompt being empty here — the scheduler already guarantees a non-empty digest via `has_finished_pending()`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_app.py -k "wake or auto_depth" -v`
Expected: PASS (all five new tests)

- [ ] **Step 5: Run the full app suite (no regressions)**

Run: `uv run pytest tests/test_app.py -v`
Expected: PASS — confirm the existing job-panel tests (`test_job_panel_hidden_until_jobs_then_live_updates`, `test_job_panel_reflects_jobs_on_mount`) still pass; they register/cancel jobs, which now also call `_maybe_wake` (a no-op for cancelled/running jobs and, in those tests, `run_worker` is real but no digest fires an unwanted turn because cancellation, not completion, settles them — verify, and if a test now sees an extra turn, it indicates a real wiring bug to fix in `_maybe_wake`, not a test to weaken).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py tests/test_app.py
git commit -m "feat(tui): autonomous wake-on-completion scheduler with depth cap

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 5: `/jobs` command (Component E)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/commands.py` (add `_cmd_jobs`, register in `COMMANDS`)
- Test: `tests/test_commands.py`

**Interfaces:**
- Consumes: `app.harness.deps.jobs` (`JobRegistry` — `.list()`, `.output(id)`, `await .cancel(id)`), `marim_harness.jobs.render_jobs`, `app.autonomous_wake` (Task 4), `app.post_system` (async), and the `Command` dataclass + `COMMANDS`/`COMMANDS_BY_NAME` registry already in `commands.py`.
- Produces: a `jobs` command:
  - `/jobs` → list jobs via `render_jobs(app.harness.deps.jobs.list())` (or a "No background jobs." message when empty).
  - `/jobs output <id>` → `app.harness.deps.jobs.output(id)`.
  - `/jobs cancel <id>` → `await app.harness.deps.jobs.cancel(id)`.
  - `/jobs wake on|off` → set `app.autonomous_wake` and confirm; bare `/jobs wake` reports current state.

- [ ] **Step 1: Write the failing tests**

Add to `tests/test_commands.py` (it already imports `COMMANDS_BY_NAME`, `dispatch`, and uses `_FakeApp`). The `_FakeApp` there has `post_system` and a `harness = SimpleNamespace(...)`; these tests give it a real `JobRegistry`:

```python
@pytest.mark.anyio
async def test_jobs_command_registered():
    assert "jobs" in COMMANDS_BY_NAME


@pytest.mark.anyio
async def test_jobs_lists_running_jobs():
    import asyncio

    from marim_harness.jobs import JobRegistry

    async def slow():
        await asyncio.sleep(5)
        return "x"

    reg = JobRegistry()
    reg.register("agent", "explore: map", slow())
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs")
    out = app.posted[-1]
    assert "job-1" in out and "explore: map" in out
    await reg.cancel_all()


@pytest.mark.anyio
async def test_jobs_empty_reports_none():
    from marim_harness.jobs import JobRegistry

    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=JobRegistry()))
    await dispatch(app, "/jobs")
    assert "No background jobs" in app.posted[-1]


@pytest.mark.anyio
async def test_jobs_output_prints_result():
    from marim_harness.jobs import JobRegistry

    async def quick():
        return "the report body"

    reg = JobRegistry()
    job_id = reg.register("agent", "a", quick())
    await reg.wait(job_id)
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs output job-1")
    assert "the report body" in app.posted[-1]


@pytest.mark.anyio
async def test_jobs_cancel_cancels_job():
    import asyncio

    from marim_harness.jobs import JobRegistry

    async def slow():
        await asyncio.sleep(5)
        return "x"

    reg = JobRegistry()
    job_id = reg.register("bash", "sleep", slow())
    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=reg))
    await dispatch(app, "/jobs cancel job-1")
    assert reg.get(job_id).status == "cancelled"
    assert "cancel" in app.posted[-1].lower()


@pytest.mark.anyio
async def test_jobs_wake_toggles_app_flag():
    app = _FakeApp()
    app.autonomous_wake = True
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=None))
    await dispatch(app, "/jobs wake off")
    assert app.autonomous_wake is False
    assert "off" in app.posted[-1].lower()
    await dispatch(app, "/jobs wake on")
    assert app.autonomous_wake is True
    assert "on" in app.posted[-1].lower()


@pytest.mark.anyio
async def test_jobs_unknown_subcommand_shows_usage():
    from marim_harness.jobs import JobRegistry

    app = _FakeApp()
    app.harness = SimpleNamespace(deps=SimpleNamespace(jobs=JobRegistry()))
    await dispatch(app, "/jobs frobnicate")
    assert "usage" in app.posted[-1].lower()
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_commands.py -k jobs -v`
Expected: FAIL — `test_jobs_command_registered` fails (`"jobs"` not in `COMMANDS_BY_NAME`), and `dispatch(app, "/jobs")` reports an unknown command.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/interfaces/tui/commands.py`, add the handler (model it on the existing `_cmd_worktree` subcommand-parsing template at lines 304–359 — `sub, _, rest = arg.strip().partition(" ")`):

```python
async def _cmd_jobs(app: "HarnessApp", arg: str) -> None:
    from ...jobs import render_jobs

    jobs = app.harness.deps.jobs
    sub, _, rest = arg.strip().partition(" ")
    rest = rest.strip()
    if sub in ("", "list"):
        rendered = render_jobs(jobs.list())
        await app.post_system(rendered or "No background jobs.")
    elif sub == "output":
        if not rest:
            await app.post_system("Usage: /jobs output <id>")
            return
        await app.post_system(jobs.output(rest))
    elif sub == "cancel":
        if not rest:
            await app.post_system("Usage: /jobs cancel <id>")
            return
        await app.post_system(await jobs.cancel(rest))
    elif sub == "wake":
        if rest in ("on", "off"):
            app.autonomous_wake = rest == "on"
            await app.post_system(f"Autonomous wake: {rest}.")
        elif rest == "":
            state = "on" if app.autonomous_wake else "off"
            await app.post_system(
                f"Autonomous wake is {state}. Use `/jobs wake on|off` to change it."
            )
        else:
            await app.post_system("Usage: /jobs wake [on|off]")
    else:
        await app.post_system(
            "Usage: /jobs [list | output <id> | cancel <id> | wake [on|off]]"
        )
```

Register it in the `COMMANDS` list (lines 370–391):

```python
    Command(
        "jobs",
        "background jobs: /jobs [list | output <id> | cancel <id> | wake [on|off]]",
        _cmd_jobs,
    ),
```

> **Implementer note:** match the existing import style at the top of `_cmd_jobs`
> to how `_cmd_worktree` imports (`from ...workspace.worktree import ...`) — the
> `jobs` module is at `marim_harness.jobs`, so from `interfaces/tui/commands.py`
> the relative import is `from ...jobs import render_jobs`. If `commands.py`
> references `HarnessApp` only under `TYPE_CHECKING`, keep the `"HarnessApp"`
> annotation as a string (as written above).

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_commands.py -k jobs -v`
Expected: PASS (all seven new tests)

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/interfaces/tui/commands.py tests/test_commands.py
git commit -m "feat(tui): add /jobs command (list/output/cancel/wake toggle)

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

---

### Task 6: Final gates + help discoverability

**Files:**
- Possibly modify: the `/help` text source if it enumerates commands and an assertion requires `/jobs` to appear (check `test_slash_help_lists_commands` in `tests/test_app.py` — it asserts `/mode` and `/clear`; adding `/jobs` to help is optional unless a test demands it).
- Test: the whole suite.

**Interfaces:**
- Consumes: everything from Tasks 1–5.
- Produces: a green tree across the three gates.

- [ ] **Step 1: Run the linter**

Run: `uv run ruff check src tests`
Expected: PASS (no findings). Fix any import-order / unused-import findings the new code introduced.

- [ ] **Step 2: Run the type checker**

Run: `uv run pyright src`
Expected: PASS. Common fixes if it complains: ensure `HarnessConfig` field types are annotated (`autonomous_wake: bool`, `wake_depth_cap: int`); ensure `_maybe_wake` has a `-> None` return.

- [ ] **Step 3: Run the full test suite**

Run: `uv run pytest`
Expected: PASS. If `tests/test_app.py::test_slash_help_lists_commands` (or any help test) now fails because it asserts on a command set, that is a real discoverability gap — add `jobs` to the help output rather than weakening the test.

- [ ] **Step 4: Commit any gate fixes**

```bash
git add -- <only the files you changed>
git commit -m "chore: satisfy ruff/pyright/pytest gates for wake feature

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01J1DGg5LFX9aBnYM56y1j5x"
```

(If Steps 1–3 are already green with no changes, skip this commit.)

---

## Self-Review

**1. Spec coverage**

| Spec section | Task |
|--------------|------|
| Component A — Wake scheduler (TUI) | Task 4 |
| `JobRegistry.has_finished_pending()` predicate | Task 1 |
| Component B — Autonomous turn entry (`_run_turn("")`) | Task 4 (Step 3, the `run_worker(self._run_turn(""))` call) |
| Component C — Usage-race fix | **Dropped** (spec records the single-threaded-asyncio rationale; out of scope) |
| Component D — Background sub-agent task isolation | Task 3 |
| Component E — `/jobs` command | Task 5 |
| Defaults: depth cap 3, wake on | Task 2 (config) + Task 4 (seeded into the app) |
| Loop guard: depth cap + kill switch | Task 4 (`_wake_depth_cap`, `autonomous_wake`) + Task 5 (`/jobs wake`) |
| Depth reset on user turn | Task 4 (Step 3, `on_prompt_input_submitted`) |
| Queue-when-busy / drain-after-turn | Task 4 (`_turn_worker` guard + `_run_turn` finally) |
| Batching (one turn drains all) | Inherent — `take_finished_digest()` drains the whole set in one turn |
| Gates green | Task 6 |

No gaps. Component C is intentionally absent.

**2. Placeholder scan**

No "TBD"/"handle edge cases"/"similar to Task N" placeholders. Every code step shows complete code. Two implementer notes flag *verification* of real method names (`TaskList` accessor, `HarnessApp` typing) with explicit fallbacks — these are guardrails, not deferred work.

**3. Type consistency**

- `has_finished_pending` — defined Task 1, consumed Task 4. ✓
- `autonomous_wake` / `wake_depth_cap` — `ModelConfig` (Task 2) → `HarnessConfig` (Task 2) → `Harness.autonomous_wake` / `Harness.wake_depth_cap` (Task 2) → `HarnessApp.autonomous_wake` / `HarnessApp._wake_depth_cap` (Task 4). Note the app stores the cap as the private `_wake_depth_cap`; the public toggle stays `autonomous_wake`. The `/jobs wake` handler (Task 5) flips `app.autonomous_wake` — name matches. ✓
- `_maybe_wake` / `_auto_turn_depth` / `_turn_worker` — all defined and read in Task 4. ✓
- `render_jobs`, `JobRegistry.list/output/cancel` — pre-existing, used unchanged in Task 5. ✓

Consistent throughout.

## Execution Handoff

After saving this plan, the controller offers the execution choice (subagent-driven vs inline) per the writing-plans skill.
