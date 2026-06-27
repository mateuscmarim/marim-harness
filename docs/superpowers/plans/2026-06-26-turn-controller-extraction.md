# TurnController Extraction Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Extract the turn-lifecycle orchestration (the `run_turn` → `_run_with_approval` pipeline, including `_maybe_compact`, `_assemble_prompt`, the approval loop, `_flush_resumable`, and the checkpoint rollback-on-failure logic) out of `Harness` into a new `TurnController` collaborator, following the same extraction pattern the TUI already uses for `StreamRenderer`/`SessionView`/`StatusPresenter`.

**Architecture:** `TurnController` becomes a pure-orchestration object owned by `Harness`. It takes the collaborators it needs (`agent`, `session`, `checkpoints`, `hooks`, `mcp`, `deps`) as constructor params and exposes `run_turn(prompt, event_stream_handler, attachments) -> str`. `Harness.run_turn` delegates to `self.turn_controller.run_turn(...)`. The controller owns the mutable turn-state fields currently on `Harness`: `_pending_error_note`, `_pending_hook_context`, `_pending_jobs_digest`, `_consumed_this_turn`, `_active_run_ctx`, `_steer_buffer`. Harness keeps model lifecycle, session management (new/switch/rename), MCP lifecycle, LSP wiring, and `bind_ui`.

**Tech Stack:** Python 3.10+, Pydantic AI (agent.run, DeferredToolRequests, capture_run_messages), asyncio. No new dependencies.

## Global Constraints

- No new third-party deps (superpowers core invariant).
- TDD — every code change has a failing test first.
- Ruff set: E,F,I,UP,B,SIM; line length 100.
- `requires-python = ">=3.10"` — no 3.11+ syntax.
- Preserve all existing invariants documented in `agent.py` docstrings (resumability, rollback baseline, one-shot consumables, steer buffering, overflow retry).
- CI order: ruff check → pyright → pytest.

---

## Task 1: Scaffold the `TurnController` class skeleton

**Files:**
- Create: `src/marim_harness/turn_controller.py`
- Modify: `src/marim_harness/agent.py` (import only; no logic change yet)
- Test: `tests/test_turn_controller.py`

**Interfaces:**
- Consumes: Harness collaborators (agent, session, checkpoints, hooks, mcp, deps)
- Produces: `TurnController` class with `run_turn()` method signature (not yet wired)

- [ ] **Step 1: Write the failing test** — `tests/test_turn_controller.py`

```python
"""Tests for TurnController: the turn-lifecycle collaborator extracted from Harness."""
import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.turn_controller import TurnController


def _text_model():
    def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="ok")])
    return FunctionModel(fn)


class FakeSession:
    """Minimal session surface TurnController touches."""

    def __init__(self):
        self.history = []
        self.usage = None
        self.on_compact = None
        self.on_compact_start = None
        self.summarizer = None
        self.titler = None
        self.persist = lambda: None
        self.maybe_compact = lambda force=False: False
        self.maybe_autoname = lambda: None


class FakeCheckpoints:
    def snapshot(self, prompt):
        return 0
        def invalidate_after_compaction(self): ...
    def discard(self, idx): ...
    def reload(self): ...


class FakeHooks:
    async def session_start(self, source): return None
    async def user_prompt_submit(self, prompt): return None
    async def stop(self): ...
    async def notification(self, *a, **kw): ...
    async def tool_event(self, *a, **kw): ...
    def __init__(self, *a, **kw): ...


class FakeMcp:
    def live_toolsets(self): return []


class FakeDeps:
    def __init__(self):
        self.tasks = type("T", (), {"items": []})()
        self.jobs = type("J", (), {"take_finished_digest": lambda: None, "clear_history": lambda: None})()
        self.mode = None
        self.request_approval = None
        self.hooks = None
        self.interactive = False
        self.workspace_root = None
        self.notifier = None
        self.services = None
        self.on_subagent_event = None
        self.on_subagent_notice = None
        self.on_subagent_usage = None
        self.on_subagent_model = None
        self.on_tasks_changed = None
        self.on_jobs_changed = None
        self.on_compact = None
        self.on_compact_start = None


def test_turn_controller_accepts_collaborators(tmp_path):
    """TurnController stores its collaborators and exposes a run_turn method."""
    from pathlib import Path as _Path
    import marim_harness.agent as agent_mod

    deps = FakeDeps()
    deps.workspace_root = tmp_path
    session = FakeSession()
    cp = FakeCheckpoints()
    hooks = FakeHooks()
    mcp = FakeMcp()

    agent = agent_mod.build_collaborators(
        _text_model(),
        None,  # provider unused for this test
        deps,
        instructions="test",
        config=agent_mod.HarnessConfig(),
        get_model=lambda: None,
    ).agent

    tc = TurnController(
        agent=agent, session=session, checkpoints=cp,
        hooks=hooks, mcp=mcp, deps=deps,
    )
    assert tc is not None
    assert hasattr(tc, "run_turn")
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turn_controller.py::test_turn_controller_accepts_collaborators -v`
Expected: FAIL — `TurnController` not importable

- [ ] **Step 3: Write minimal implementation** — `src/marim_harness/turn_controller.py`

```python
"""Turn-lifecycle orchestration: the run_turn → approval loop → persist pipeline.

Extracted from Harness to isolate the most complex, highest-cyclomatic-load
subsystem (approval rounds, overflow retry, resumable flush, one-shot
consumables, steer buffering) from model/session/MCP lifecycle management.
"""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from pydantic_ai import Agent

    from .deps import Deps
    from .hooks.dispatch import TurnHooks
    from .mcp import McpManager
    from .session import SessionController
    from .session.checkpoints import CheckpointManager


class TurnController:
    """Drives one user turn to completion through approval rounds.

    Owns the mutable turn-state that formerly lived on ``Harness``:
    pending error notes, hook context, jobs digest, steer buffer, and the
    active RunContext for mid-turn steering.
    """

    def __init__(
        self,
        agent: Agent,
        session: SessionController,
        checkpoints: CheckpointManager,
        hooks: TurnHooks,
        mcp: McpManager,
        deps: Deps,
    ) -> None:
        self.agent = agent
        self.session = session
        self.checkpoints = checkpoints
        self.hooks = hooks
        self.mcp = mcp
        self.deps = deps

        # One-shot turn state (consumed by _assemble_prompt, restored on failure).
        self._pending_error_note: str | None = None
        self._pending_hook_context: str | None = None
        self._pending_jobs_digest: str | None = None
        self._consumed_this_turn: tuple[str | None, str | None] = (None, None)

        # Live RunContext for mid-turn steering.
        self._active_run_ctx: Any = None
        self._steer_buffer: list[tuple[str, list[tuple[bytes, str]] | None]] = []

    async def run_turn(
        self,
        prompt: str,
        event_stream_handler=None,
        attachments: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Run the agent until it produces a final text answer.

        Delegates to the orchestration pipeline. Currently a thin wrapper
        that will absorb the logic from ``Harness.run_turn`` in Task 2.
        """
        # Placeholder — logic moves here in Task 2.
        return await self._run_with_approval(
            prompt, deferred_results=None,
            toolsets=self.mcp.live_toolsets(),
            event_stream_handler=event_stream_handler,
        )

    async def _run_with_approval(
        self,
        user_prompt,
        deferred_results,
        toolsets,
        event_stream_handler,
        resumable,
    ) -> str:
        """Drive the agent.run loop. Logic moves here from Harness in Task 2."""
        raise NotImplementedError
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_turn_controller.py::test_turn_controller_accepts_collaborators -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/turn_controller.py tests/test_turn_controller.py
git commit -m "feat: scaffold TurnController collaborator"
```

---

## Task 2: Move `_run_with_approval` and `run_turn` logic into TurnController

**Files:**
- Modify: `src/marim_harness/turn_controller.py`
- Modify: `src/marim_harness/agent.py` (Harness.run_turn delegates; remove moved methods)
- Test: `tests/test_turn_controller.py` (new tests for approval loop behavior)

**Interfaces:**
- Consumes: The `TurnController` skeleton from Task 1
- Produces: `TurnController` with full `run_turn` / `_run_with_approval` logic; `Harness.run_turn` is a thin delegate

- [ ] **Step 1: Write failing tests for approval-loop behaviors**

Add to `tests/test_turn_controller.py`:

```python
@pytest.mark.anyio
async def test_failed_turn_preserves_user_prompt(tmp_path):
    """When a run_turn raises, the user's prompt survives in history."""
    from pydantic_ai.models.function import FunctionModel as FM
    from marim_harness.agent import HarnessConfig, build_collaborators
    from marim_harness.deps import Deps
    from marim_harness.permissions import Mode
    from marim_harness.tools.provider import BuiltinToolProvider

    def raising_model(messages, info):
        raise RuntimeError("turn boom")

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    collab = build_collaborators(
        FM(raising_model), BuiltinToolProvider(), deps, "test",
        HarnessConfig(), get_model=lambda: None,
    )
    tc = TurnController(
        agent=collab.agent, session=collab.session,
        checkpoints=collab.checkpoints, hooks=collab.hooks,
        mcp=collab.mcp, deps=deps,
    )
    with pytest.raises(RuntimeError):
        await tc.run_turn("please remember this")
    user_texts = [
        p.content
        for m in collab.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert any("please remember this" in str(t) for t in user_texts)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turn_controller.py::test_failed_turn_preserves_user_prompt -v`
Expected: FAIL — `_run_with_approval` raises `NotImplementedError`

- [ ] **Step 3: Move logic from Harness to TurnController**

Move these methods and their helpers from `agent.py` into `turn_controller.py`:
- `_run_with_approval` (lines 741–885)
- `run_turn` (lines 887–954) — becomes a thin orchestrator that calls helpers
- `_maybe_compact` (lines 515–522)
- `_maybe_autoname` (lines 524–525)
- `_flush_resumable` (lines 527–551)
- `_assemble_prompt` (lines 629–683)
- `_build_hooked_handler` (lines 712–739)
- `steer` / `_flush_steers` / `take_buffered_steers` (lines 685–710)

Also move the module-level helpers these depend on:
- `_has_unanswered_tool_calls`, `_drop_nameless_tool_calls`, `_repair_unanswered_tool_calls`, `_turn_produced_response`
- `_INTERRUPTED_TOOL_NOTE`

In `agent.py`:
- Remove all moved methods
- `Harness.run_turn` becomes: `return await self.turn_controller.run_turn(prompt, event_stream_handler, attachments)`
- `Harness.__init__` creates `self.turn_controller = TurnController(...)` after building collaborators
- Remove moved fields from `Harness.__init__` (they now live on `TurnController`)

Key wiring: `TurnController.__init__` receives `get_model` as a callable (like `build_collaborators` does) so it can pass `self.agent` and `current_model` to `_run_with_approval`. The `Harness` exposes a `get_model` closure that the controller uses.

- [ ] **Step 4: Run full agent test suite to verify no regressions**

Run: `uv run pytest tests/test_agent.py tests/test_agent_consumables.py tests/test_agent_checkpoints.py tests/test_agent_subagents.py -v`
Expected: ALL PASS

- [ ] **Step 5: Run full test suite**

Run: `uv run pytest --no-cov`
Expected: ALL PASS

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/turn_controller.py src/marim_harness/agent.py tests/test_turn_controller.py
git commit -m "refactor: move turn lifecycle orchestration into TurnController"
```

---

## Task 3: Move remaining turn-state fields off Harness

**Files:**
- Modify: `src/marim_harness/agent.py`
- Modify: `src/marim_harness/interfaces/tui/app.py` (update any direct reads of moved fields)
- Test: `tests/test_agent.py`

**Interfaces:**
- Consumes: TurnController from Task 2
- Produces: Harness no longer has `_pending_error_note`, `_pending_hook_context`, `_pending_jobs_digest`, `_consumed_this_turn`, `_active_run_ctx`, `_steer_buffer`

- [ ] **Step 1: Write failing test for field absence**

```python
def test_harness_has_no_turn_state_fields(tmp_path):
    """Harness no longer owns mutable turn-state — it lives on TurnController."""
    h = _minimal_harness(tmp_path)
    assert not hasattr(h, "_pending_error_note") or h._pending_error_note is None
    assert not hasattr(h, "_pending_hook_context") or h._pending_hook_context is None
    assert not hasattr(h, "_pending_jobs_digest") or h._pending_jobs_digest is None
    # The controller owns these now:
    assert hasattr(h, "turn_controller")
    assert h.turn_controller._pending_error_note is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_turn_controller.py::test_harness_has_no_turn_state_fields -v`
Expected: FAIL

- [ ] **Step 3: Remove moved fields from Harness**

In `agent.py`:
- Remove `_pending_error_note`, `_pending_hook_context`, `_pending_jobs_digest`, `_consumed_this_turn`, `_active_run_ctx`, `_steer_buffer` from `Harness.__init__`
- Remove `steer()`, `_flush_steers()`, `take_buffered_steers()` from `Harness` (they now live on `TurnController`)
- Update `Harness.bind_ui` — it no longer needs to set `deps.interactive` based on turn state (that flag stays on Deps, set elsewhere if needed)
- Check `tui/app.py` for any direct reads of `harness._pending_error_note` etc. — update to `harness.turn_controller._pending_error_note` or (better) add passthrough properties on Harness

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest --no-cov`
Expected: ALL PASS

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/agent.py src/marim_harness/turn_controller.py
git commit -m "refactor: remove turn-state fields from Harness, owned by TurnController"
```

---

## Task 4: Lint, type-check, and review

**Files:**
- None (verification only)

**Interfaces:**
- Consumes: Clean codebase from Task 3
- Produces: CI-green codebase ready for review

- [ ] **Step 1: Run ruff**

Run: `uv run ruff check src/marim_harness/turn_controller.py src/marim_harness/agent.py tests/test_turn_controller.py`
Expected: no errors

- [ ] **Step 2: Run pyright**

Run: `uv run pyright`
Expected: no errors in changed files

- [ ] **Step 3: Run full test suite with coverage**

Run: `uv run pytest`
Expected: ALL PASS, coverage maintained

- [ ] **Step 4: Dispatch claude-deep for review**

Spawn a `claude-deep` agent to review the diff against the architecture review findings. The agent should verify:
1. All resumability invariants are preserved
2. No logic was lost in the move (diff is purely structural)
3. The `Harness` class shrank by ~200-300 lines
4. No new coupling introduced

- [ ] **Step 5: Commit review sign-off**

If the reviewer finds issues, fix and re-review. If clean:
```bash
git commit --allow-empty -m "chore: TurnController extraction reviewed and approved"
```

---

## Self-Review Checklist

- [ ] Spec coverage: All 4 tasks extract the full turn-lifecycle pipeline
- [ ] No placeholders: Every step has concrete code
- [ ] Type consistency: `run_turn` signature matches between Harness and TurnController
- [ ] All existing tests pass without modification (the public API — `harness.run_turn()` — is unchanged)
- [ ] `Harness` line count decreased measurably
- [ ] No new imports or deps added
