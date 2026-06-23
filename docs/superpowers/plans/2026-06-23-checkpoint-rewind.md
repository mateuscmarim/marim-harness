# Checkpoint / Rewind Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Let the user rewind a session to an earlier turn — restoring both the conversation history and the working-tree files to the state they had before that turn ran.

**Architecture:** A per-session list of `Checkpoint` records is captured at the start of every turn. Each records the conversation length and (when the workspace is a git repo) a shadow git commit of the working tree, written under a private `refs/marim/checkpoints/...` ref that never touches the user's branch, index, or HEAD. Rewinding truncates `session.history` and restores the working tree from the shadow commit. The work splits into two independently shippable phases: **Phase A** (conversation-only rewind, no git) and **Phase B** (git-backed file snapshots).

**Tech Stack:** Python 3.10+, Pydantic AI, Textual, `git` plumbing via `subprocess`, pytest + `anyio`.

## Global Constraints

- Python floor: `>=3.10`. Use `Optional[X]` / `X | None`, not bare generics.
- Lint/type gates must stay green: `ruff check .`, `pyright src` (0 errors), `pytest`.
- Ruff line length: 100.
- New git plumbing must NEVER mutate the user's branch, working index, or HEAD — only `refs/marim/checkpoints/*` and (on restore) working-tree files.
- A malformed/missing checkpoint sidecar must never raise into a turn — degrade to "no checkpoints", mirroring how `SessionStore`/hook config fail safe.
- Snapshot/restore must be a no-op (not an error) when the workspace is not a git repo.
- Commit messages end with the repo's trailer block (see existing history); commit after each task.

---

## File Structure

- **Create `src/marim_harness/session/checkpoints.py`** — the `Checkpoint` dataclass, the `Snapshotter` protocol + `NullSnapshotter`, and `CheckpointManager` (capture/list/rewind/reload/clear + sidecar JSON persistence). No git here; git is injected via a `Snapshotter`.
- **Create `src/marim_harness/workspace/snapshot.py`** — `GitSnapshotter`: the shadow-ref git plumbing (capture working tree → commit, restore working tree from commit, delete ref). Mirrors `workspace/worktree.py`'s subprocess style and reuses its public `repo_root`.
- **Modify `src/marim_harness/agent.py`** — construct a `CheckpointManager` on the `Harness`, call `snapshot()` at the top of `run_turn`, and `reload()`/`clear()` it across the session-lifecycle methods.
- **Modify `src/marim_harness/interfaces/tui/app.py`** — add `rewind_to_checkpoint(index)` which calls the manager then re-renders the log.
- **Modify `src/marim_harness/interfaces/tui/commands.py`** — add the `/rewind` command (list with no arg, rewind with an index).
- **Modify `README.md`** — document the feature and its git/.gitignore boundaries.
- **Create `tests/test_checkpoints.py`** — unit tests for `Checkpoint` and `CheckpointManager` with a test-double session and the `NullSnapshotter`.
- **Create `tests/test_snapshot.py`** — real-git tests for `GitSnapshotter`.
- **Modify `tests/test_agent.py`** (or a new `tests/test_agent_checkpoints.py`) — end-to-end: a turn creates a checkpoint; rewind truncates history.

---

# Phase A — Conversation-only rewind (no git)

Ships working software: `/rewind` restores conversation history. File restore is wired in Phase B by swapping the injected snapshotter.

### Task 1: Checkpoint data model + snapshotter seam

**Files:**
- Create: `src/marim_harness/session/checkpoints.py`
- Test: `tests/test_checkpoints.py`

**Interfaces:**
- Produces:
  - `Checkpoint` dataclass: fields `index: int`, `history_len: int`, `commit: Optional[str]`, `created: str`, `prompt_preview: str`; methods `to_dict() -> dict` and classmethod `from_dict(d: dict) -> "Checkpoint"`.
  - `Snapshotter` Protocol: `capture(self, ref: str, message: str) -> Optional[str]`, `restore(self, commit: str) -> None`, `delete(self, ref: str) -> None`.
  - `NullSnapshotter` implementing `Snapshotter` with no-op methods (`capture` returns `None`).

- [ ] **Step 1: Write the failing test**

```python
# tests/test_checkpoints.py
from marim_harness.session.checkpoints import Checkpoint, NullSnapshotter


def test_checkpoint_roundtrips_through_dict():
    cp = Checkpoint(
        index=2, history_len=6, commit="abc123",
        created="2026-06-23T00:00:00+00:00", prompt_preview="fix the bug",
    )
    assert Checkpoint.from_dict(cp.to_dict()) == cp


def test_from_dict_tolerates_missing_optional_commit():
    d = {"index": 0, "history_len": 0, "created": "t", "prompt_preview": ""}
    assert Checkpoint.from_dict(d).commit is None


def test_null_snapshotter_captures_nothing():
    assert NullSnapshotter().capture("refs/x", "msg") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checkpoints.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: marim_harness.session.checkpoints`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/session/checkpoints.py
"""Per-session checkpoints: a capture of conversation length + an optional
shadow git commit of the working tree, taken at the start of each turn so a
session can be rewound to an earlier point.

The git work is injected as a ``Snapshotter`` so this module stays
git-agnostic and unit-testable; the real implementation lives in
``workspace/snapshot.py``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Checkpoint:
    index: int            # monotonic ordinal, unique within a session
    history_len: int      # len(history) captured before this turn ran
    commit: Optional[str] # shadow commit sha (restore target), or None
    created: str          # ISO-8601 UTC timestamp
    prompt_preview: str   # first ~80 chars of the turn's user prompt

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "history_len": self.history_len,
            "commit": self.commit,
            "created": self.created,
            "prompt_preview": self.prompt_preview,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Checkpoint":
        return cls(
            index=int(d["index"]),
            history_len=int(d["history_len"]),
            commit=d.get("commit"),
            created=str(d.get("created", "")),
            prompt_preview=str(d.get("prompt_preview", "")),
        )


class Snapshotter(Protocol):
    """Captures/restores the working tree behind a checkpoint. The Null
    implementation makes conversation-only rewind work with no git."""

    def capture(self, ref: str, message: str) -> Optional[str]: ...
    def restore(self, commit: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class NullSnapshotter:
    """No-op snapshotter: checkpoints carry no file state."""

    def capture(self, ref: str, message: str) -> Optional[str]:
        return None

    def restore(self, commit: str) -> None:
        pass

    def delete(self, ref: str) -> None:
        pass
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_checkpoints.py -q --no-cov`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/session/checkpoints.py tests/test_checkpoints.py
git commit -m "feat(checkpoints): Checkpoint model and snapshotter seam

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 2: CheckpointManager (capture, list, rewind, persistence)

**Files:**
- Modify: `src/marim_harness/session/checkpoints.py`
- Test: `tests/test_checkpoints.py`

**Interfaces:**
- Consumes: `Checkpoint`, `Snapshotter`, `NullSnapshotter` (Task 1). A `session` object exposing `history: list`, `set_history(list) -> None`, `persist(*, force: bool = False) -> None`, and `store` (either `None`, or an object with `.path: pathlib.Path` and `.session_id: str`).
- Produces:
  - `RewindResult` dataclass: `history_len: int`, `restored_files: bool`.
  - `CheckpointManager(session, snapshotter: Optional[Snapshotter] = None, *, limit: int = 50)`.
  - Methods: `snapshot(prompt_preview: str) -> None`, `list() -> list[Checkpoint]`, `rewind(index: int) -> RewindResult` (raises `KeyError` for an unknown index), `reload() -> None`, `clear() -> None`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_checkpoints.py  (append)
from pathlib import Path

from marim_harness.session.checkpoints import CheckpointManager, RewindResult


class _FakeStore:
    def __init__(self, path: Path, session_id: str):
        self.path = path
        self.session_id = session_id


class _FakeSession:
    """Minimal stand-in for SessionController for manager unit tests."""

    def __init__(self, store):
        self._history: list = []
        self.store = store
        self.persisted = 0

    @property
    def history(self) -> list:
        return self._history

    def set_history(self, value: list) -> None:
        self._history = value

    def persist(self, *, force: bool = False) -> None:
        self.persisted += 1


def _session(tmp_path: Path) -> _FakeSession:
    return _FakeSession(_FakeStore(tmp_path / "sess.json", "sess"))


def test_snapshot_records_history_length_and_preview(tmp_path: Path):
    s = _session(tmp_path)
    s.set_history(["m0", "m1"])
    mgr = CheckpointManager(s)
    mgr.snapshot("do the thing")
    cps = mgr.list()
    assert len(cps) == 1
    assert cps[0].index == 0
    assert cps[0].history_len == 2
    assert cps[0].prompt_preview == "do the thing"


def test_indices_increase_across_snapshots(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("a")
    s.set_history(["m0"])
    mgr.snapshot("b")
    assert [c.index for c in mgr.list()] == [0, 1]


def test_rewind_truncates_history_and_persists(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("turn-1")          # history_len 0 captured
    s.set_history(["u1", "a1", "u2", "a2"])
    result = mgr.rewind(0)
    assert isinstance(result, RewindResult)
    assert result.history_len == 0
    assert s.history == []
    assert s.persisted >= 1


def test_rewind_drops_later_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("t1")
    s.set_history(["u1", "a1"])
    mgr.snapshot("t2")
    mgr.rewind(0)
    assert [c.index for c in mgr.list()] == [0]


def test_rewind_unknown_index_raises(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    import pytest
    with pytest.raises(KeyError):
        mgr.rewind(99)


def test_checkpoints_persist_to_sidecar_and_reload(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("kept across reload")
    mgr2 = CheckpointManager(s)
    mgr2.reload()
    assert mgr2.list()[0].prompt_preview == "kept across reload"


def test_clear_empties_checkpoints(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s)
    mgr.snapshot("t1")
    mgr.clear()
    assert mgr.list() == []


def test_limit_prunes_oldest(tmp_path: Path):
    s = _session(tmp_path)
    mgr = CheckpointManager(s, limit=2)
    for i in range(4):
        s.set_history(list(range(i)))
        mgr.snapshot(f"t{i}")
    indices = [c.index for c in mgr.list()]
    assert indices == [2, 3]  # oldest two pruned, newest kept
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_checkpoints.py -q --no-cov`
Expected: FAIL — `ImportError: cannot import name 'CheckpointManager'`.

- [ ] **Step 3: Write minimal implementation**

Append to `src/marim_harness/session/checkpoints.py`:

```python
import json
import logging
from dataclasses import dataclass as _dataclass
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

_REF_PREFIX = "refs/marim/checkpoints"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@_dataclass
class RewindResult:
    history_len: int
    restored_files: bool


class CheckpointManager:
    """Owns one session's checkpoint list. Captures a checkpoint at the start
    of each turn and rewinds the session (conversation + files) to one.

    Persistence is a sidecar JSON next to the session file; with no store the
    list lives only in memory. The git side is delegated to the injected
    ``Snapshotter`` (Null by default → conversation-only rewind)."""

    def __init__(
        self, session, snapshotter: "Optional[Snapshotter]" = None, *, limit: int = 50
    ) -> None:
        self.session = session
        self.snapshotter: Snapshotter = snapshotter or NullSnapshotter()
        self.limit = limit
        self._checkpoints: list[Checkpoint] = []
        self.reload()

    # --- persistence -----------------------------------------------------

    def _sidecar_path(self) -> Optional[Path]:
        store = getattr(self.session, "store", None)
        if store is None:
            return None
        return Path(store.path).with_name(f"{store.session_id}.checkpoints.json")

    def _save(self) -> None:
        path = self._sidecar_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"checkpoints": [c.to_dict() for c in self._checkpoints]}
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)  # atomic swap, mirrors SessionStore.save
        except OSError as exc:
            logger.debug("failed to persist checkpoints: %s", exc)

    def reload(self) -> None:
        """Load the checkpoint list for the current session (called on
        resume/switch/new). A missing or corrupt sidecar yields an empty list."""
        self._checkpoints = []
        path = self._sidecar_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("ignoring unreadable checkpoint sidecar %s: %s", path, exc)
            return
        self._checkpoints = [
            Checkpoint.from_dict(d) for d in data.get("checkpoints", [])
        ]

    # --- operations ------------------------------------------------------

    def _session_id(self) -> str:
        store = getattr(self.session, "store", None)
        return getattr(store, "session_id", "anon") if store is not None else "anon"

    def _ref(self, index: int) -> str:
        return f"{_REF_PREFIX}/{self._session_id()}/{index}"

    def snapshot(self, prompt_preview: str) -> None:
        """Capture a checkpoint of the current state before a turn runs."""
        index = (self._checkpoints[-1].index + 1) if self._checkpoints else 0
        commit = self.snapshotter.capture(self._ref(index), f"marim checkpoint {index}")
        self._checkpoints.append(
            Checkpoint(
                index=index,
                history_len=len(self.session.history),
                commit=commit,
                created=_now(),
                prompt_preview=(prompt_preview or "")[:80],
            )
        )
        self._prune()
        self._save()

    def _prune(self) -> None:
        if len(self._checkpoints) <= self.limit:
            return
        dropped = self._checkpoints[: -self.limit]
        self._checkpoints = self._checkpoints[-self.limit :]
        for cp in dropped:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))

    def list(self) -> list[Checkpoint]:
        return list(self._checkpoints)

    def rewind(self, index: int) -> RewindResult:
        """Restore the session to checkpoint ``index``: truncate history, restore
        files (if the checkpoint has a commit), and drop later checkpoints.
        Raises ``KeyError`` if no checkpoint has that index."""
        cp = next((c for c in self._checkpoints if c.index == index), None)
        if cp is None:
            raise KeyError(index)
        self.session.set_history(self.session.history[: cp.history_len])
        self.session.persist(force=True)
        restored = False
        if cp.commit is not None:
            self.snapshotter.restore(cp.commit)
            restored = True
        self._checkpoints = [c for c in self._checkpoints if c.index <= index]
        self._save()
        return RewindResult(history_len=cp.history_len, restored_files=restored)

    def clear(self) -> None:
        """Drop all checkpoints (called on session reset/clear) and their refs."""
        for cp in self._checkpoints:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))
        self._checkpoints = []
        path = self._sidecar_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("failed to clear checkpoint sidecar: %s", exc)
```

Also add `Optional` to the existing import line at the top if not already imported (Task 1 imported `Optional` via `from typing import Optional, Protocol` — keep it).

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_checkpoints.py -q --no-cov`
Expected: PASS (all Task 1 + Task 2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/session/checkpoints.py tests/test_checkpoints.py
git commit -m "feat(checkpoints): CheckpointManager capture/list/rewind + sidecar

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 3: Wire CheckpointManager into the Harness

**Files:**
- Modify: `src/marim_harness/agent.py:294-298` (construct), `:324-346` (lifecycle), `:329-330` (reset), `:598-599` (snapshot in `run_turn`)
- Test: `tests/test_agent_checkpoints.py` (create)

**Interfaces:**
- Consumes: `CheckpointManager` (Task 2), the existing `SessionController` API (`history`, `set_history`, `persist`, `store`).
- Produces: `Harness.checkpoints: CheckpointManager`, populated with one checkpoint per `run_turn`.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_agent_checkpoints.py
# NOTE: in tests/conftest.py, _make_harness and _text_model are plain HELPER
# functions (not fixtures): _make_harness(model, deps) -> Harness, and
# _text_model() -> FunctionModel. Construct Deps explicitly, exactly as the
# existing tests/test_agent.py does (see line ~85).
import pytest

from marim_harness.deps import Deps
from marim_harness.permissions import Mode
from tests.conftest import _make_harness, _text_model

pytestmark = pytest.mark.anyio


async def test_turn_creates_a_checkpoint(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)
    assert harness.checkpoints.list() == []
    await harness.run_turn("first user message")
    cps = harness.checkpoints.list()
    assert len(cps) == 1
    assert cps[0].history_len == 0          # captured before the turn ran
    assert cps[0].prompt_preview.startswith("first user message")


async def test_rewind_truncates_to_before_a_turn(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)
    await harness.run_turn("turn one")
    after_one = list(harness.session.history)
    assert after_one  # non-empty
    await harness.run_turn("turn two")
    # Two checkpoints: index 0 (before turn one), index 1 (before turn two).
    harness.checkpoints.rewind(1)
    assert list(harness.session.history) == after_one
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_agent_checkpoints.py -q --no-cov`
Expected: FAIL — `AttributeError: 'Harness' object has no attribute 'checkpoints'`.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/agent.py`, add the import near the other session imports:

```python
from .session.checkpoints import CheckpointManager
```

After the `self.session = SessionController(...)` block (currently ending at line 298), add:

```python
        # Per-session checkpoints. Phase A uses the default NullSnapshotter
        # (conversation-only rewind); Phase B injects a GitSnapshotter here.
        self.checkpoints = CheckpointManager(self.session)
```

In the lifecycle methods, reload/clear the manager so it tracks the active session:

```python
    def resume(self) -> int:
        count = self.session.resume()
        self.checkpoints.reload()
        self._apply_saved_model()
        return count

    def reset(self) -> None:
        self.session.reset()
        self.checkpoints.clear()

    def new_session(self, name: Optional[str] = None) -> None:
        self.session.new_session(name)
        self.checkpoints.reload()
        if (
            self.session.store is not None
            and self.session.store.model
            and self.session.store.model != self.model_id
        ):
            self.set_model(self.session.store.model, persist=False)

    def switch_session(self, session_id: str) -> int:
        count = self.session.switch_session(session_id)
        self.checkpoints.reload()
        self._apply_saved_model()
        return count
```

In `run_turn`, capture the checkpoint right after the start-of-turn compaction (currently `await self._maybe_compact()` at line 598):

```python
        await self._maybe_compact()
        # Capture a rewind point for this turn before any work runs.
        self.checkpoints.snapshot(prompt)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_agent_checkpoints.py tests/test_checkpoints.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Run the broader suite + gates to catch regressions**

Run: `uv run pytest tests/test_agent.py tests/test_agent_sessions.py -q --no-cov && uv run ruff check src && uv run pyright src`
Expected: PASS / clean.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/agent.py tests/test_agent_checkpoints.py
git commit -m "feat(checkpoints): capture a checkpoint per turn on the Harness

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 4: `/rewind` command + TUI re-render

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py` (add `rewind_to_checkpoint`)
- Modify: `src/marim_harness/interfaces/tui/commands.py` (add `_cmd_rewind` + `COMMANDS` entry)
- Test: `tests/test_app.py` (append)

**Interfaces:**
- Consumes: `Harness.checkpoints` (Task 3); `SessionView.render_session(note: str)` for re-render; `app.status.busy`; `app.post_system(str)`.
- Produces: `HarnessApp.rewind_to_checkpoint(index: int) -> None`; `/rewind` slash command.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_app.py  (append; reuses _app and the AssistantMessage import style already in this file)
@pytest.mark.anyio
async def test_rewind_command_truncates_and_rerenders(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        # Seed two checkpoints by hand against the live manager.
        mgr = app.harness.checkpoints
        mgr.snapshot("turn one")                       # index 0, history_len 0
        app.harness.session.set_history(["u1", "a1"])
        mgr.snapshot("turn two")                       # index 1, history_len 2
        app.harness.session.set_history(["u1", "a1", "u2", "a2"])

        await app.rewind_to_checkpoint(0)
        assert app.harness.session.history == []
        assert [c.index for c in mgr.list()] == [0]


@pytest.mark.anyio
async def test_rewind_command_refuses_while_busy(tmp_path: Path):
    app = _app(tmp_path)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        app.harness.checkpoints.snapshot("t1")
        app.harness.session.set_history(["u1", "a1"])
        app.status.set_busy(True)
        await app.rewind_to_checkpoint(0)
        # Busy → refused, history untouched.
        assert app.harness.session.history == ["u1", "a1"]
        app.status.set_busy(False)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_app.py -k rewind -q --no-cov`
Expected: FAIL — `AttributeError: 'HarnessApp' object has no attribute 'rewind_to_checkpoint'`.

- [ ] **Step 3: Write minimal implementation**

In `src/marim_harness/interfaces/tui/app.py`, alongside `switch_to_session_id` (near line 374):

```python
    async def rewind_to_checkpoint(self, index: int) -> None:
        """Rewind the session to checkpoint ``index`` and rebuild the log.
        Refused mid-turn — rewinding under a running turn would race history."""
        if self.status.busy:
            await self.post_system("Can't rewind while a turn is running. Press Esc first.")
            return
        try:
            result = self.harness.checkpoints.rewind(index)
        except KeyError:
            await self.post_system(f"No checkpoint #{index}. Try `/rewind` to list them.")
            return
        note = f"rewound to checkpoint #{index}"
        if result.restored_files:
            note += " (files restored)"
        await self.session.render_session(note)
        self.status.refresh_status()
```

In `src/marim_harness/interfaces/tui/commands.py`, add the handler (place it near `_cmd_switch`):

```python
async def _cmd_rewind(app: HarnessApp, arg: str) -> None:
    arg = arg.strip()
    cps = app.harness.checkpoints.list()
    if not arg:
        if not cps:
            await app.post_system("No checkpoints yet — they're captured at the start of each turn.")
            return
        lines = ["Checkpoints (newest last):"]
        for c in cps:
            preview = c.prompt_preview or "(no prompt)"
            lines.append(f"- `{c.index}` — {preview}")
        lines.append("\nRewind with `/rewind <number>`.")
        await app.post_system("\n".join(lines))
        return
    if not arg.isdigit():
        await app.post_system("Usage: `/rewind [number]`. Run `/rewind` to list checkpoints.")
        return
    await app.rewind_to_checkpoint(int(arg))
```

Add the command to the `COMMANDS` list (after the `switch` entry):

```python
    Command("rewind", "rewind to an earlier turn: /rewind [number]", _cmd_rewind),
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_app.py -k rewind -q --no-cov`
Expected: PASS (2 passed).

- [ ] **Step 5: Run gates**

Run: `uv run ruff check src tests && uv run pyright src`
Expected: clean / 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/interfaces/tui/app.py src/marim_harness/interfaces/tui/commands.py tests/test_app.py
git commit -m "feat(tui): /rewind command restores an earlier turn

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

**Phase A is now shippable: `/rewind` restores conversation history.**

---

# Phase B — Git-backed file snapshots

Swaps the `NullSnapshotter` for a `GitSnapshotter` so checkpoints also capture and restore working-tree files.

### Task 5: GitSnapshotter.capture

**Files:**
- Create: `src/marim_harness/workspace/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `repo_root` from `workspace/worktree.py` (`repo_root(path: Path) -> Path | None`).
- Produces: `GitSnapshotter(workspace_root: Path)` with `capture(self, ref: str, message: str) -> Optional[str]` returning the new commit sha (or `None` when not a git repo / on git failure). The commit is reachable from `ref` so git GC won't collect it.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot.py
import subprocess
from pathlib import Path

from marim_harness.workspace.snapshot import GitSnapshotter


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args], cwd=repo, capture_output=True, text=True, check=True
    ).stdout.strip()


def _init_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "ws"
    repo.mkdir()
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@t")
    _git(repo, "config", "user.name", "t")
    (repo / "a.txt").write_text("one\n")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-qm", "init")
    return repo


def test_capture_returns_commit_and_sets_ref(tmp_path: Path):
    repo = _init_repo(tmp_path)
    (repo / "a.txt").write_text("changed\n")
    (repo / "new.txt").write_text("fresh\n")  # untracked, non-ignored
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    assert commit
    # The ref resolves to the commit, keeping it alive.
    assert _git(repo, "rev-parse", "refs/marim/checkpoints/s/0") == commit
    # The snapshot tree contains both the modified and the untracked file.
    listed = _git(repo, "ls-tree", "-r", "--name-only", commit)
    assert "a.txt" in listed and "new.txt" in listed


def test_capture_does_not_touch_user_branch_or_index(tmp_path: Path):
    repo = _init_repo(tmp_path)
    head_before = _git(repo, "rev-parse", "HEAD")
    (repo / "a.txt").write_text("changed\n")
    GitSnapshotter(repo).capture("refs/marim/checkpoints/s/0", "cp 0")
    assert _git(repo, "rev-parse", "HEAD") == head_before          # HEAD unmoved
    assert _git(repo, "status", "--porcelain")                     # change still unstaged/dirty
    assert "changed" in (repo / "a.txt").read_text()               # working tree untouched


def test_capture_returns_none_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    assert GitSnapshotter(plain).capture("refs/marim/checkpoints/s/0", "cp") is None
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snapshot.py -q --no-cov`
Expected: FAIL — `ModuleNotFoundError: marim_harness.workspace.snapshot`.

- [ ] **Step 3: Write minimal implementation**

```python
# src/marim_harness/workspace/snapshot.py
"""Shadow git snapshots for checkpoints. Captures the working tree into a
commit under a private ``refs/marim/checkpoints/*`` ref — without touching the
user's branch, index, or HEAD — and restores the working tree from one.

This is the file-state half of a checkpoint; the conversation half lives in
``session/checkpoints.py``. Like ``worktree.py``, it is the only place (besides
that module) that shells out to git, and it never mutates user-visible git
state except working-tree files on restore."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .worktree import repo_root

logger = logging.getLogger(__name__)


@contextmanager
def _temp_index() -> Iterator[str]:
    """A throwaway git index file, so staging never touches the user's index."""
    fd, name = tempfile.mkstemp(suffix=".marim-index")
    os.close(fd)
    os.unlink(name)  # git wants to create it itself; we only need a unique path
    try:
        yield name
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


class GitSnapshotter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)

    def _repo(self) -> Optional[Path]:
        return repo_root(self.workspace_root)

    def _run(self, repo: Path, *args: str, env: Optional[dict] = None) -> str:
        return subprocess.run(
            ["git", *args], cwd=repo, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def capture(self, ref: str, message: str) -> Optional[str]:
        repo = self._repo()
        if repo is None:
            return None
        try:
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                # Stage the whole working tree (tracked + untracked, honoring
                # .gitignore) into the throwaway index, then snapshot it.
                self._run(repo, "add", "-A", env=env)
                tree = self._run(repo, "write-tree", env=env)
                commit = self._run(repo, "commit-tree", tree, "-m", message, env=env)
            # Keep the commit reachable so GC won't drop it.
            self._run(repo, "update-ref", ref, commit)
            return commit
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint capture failed: %s", exc.stderr or exc)
            return None
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snapshot.py -q --no-cov`
Expected: PASS (3 passed).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): GitSnapshotter.capture writes a shadow working-tree commit

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 6: GitSnapshotter.restore (modify, delete, create-after)

**Files:**
- Modify: `src/marim_harness/workspace/snapshot.py`
- Test: `tests/test_snapshot.py`

**Interfaces:**
- Consumes: `GitSnapshotter` (Task 5).
- Produces: `GitSnapshotter.restore(self, commit: str) -> None` — sets the working tree to match `commit`: restores modified/deleted files and removes files created after the snapshot. Before restoring, writes a safety snapshot to `refs/marim/checkpoints/_pre_restore` so the pre-rewind state is itself recoverable. No-op when not a git repo.

- [ ] **Step 1: Write the failing test**

```python
# tests/test_snapshot.py  (append)
def test_restore_reverts_modification(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").write_text("MODIFIED\n")
    snap.restore(commit)
    assert (repo / "a.txt").read_text() == "one\n"


def test_restore_recreates_deleted_file(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").unlink()
    snap.restore(commit)
    assert (repo / "a.txt").read_text() == "one\n"


def test_restore_removes_file_created_after_checkpoint(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "after.txt").write_text("should be gone\n")
    snap.restore(commit)
    assert not (repo / "after.txt").exists()


def test_restore_writes_pre_restore_safety_snapshot(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    commit = snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    (repo / "a.txt").write_text("DANGER\n")
    snap.restore(commit)
    # The pre-restore state is recoverable from the safety ref.
    pre = _git(repo, "rev-parse", "refs/marim/checkpoints/_pre_restore")
    blob = _git(repo, "show", f"{pre}:a.txt")
    assert blob == "DANGER"


def test_restore_is_noop_outside_git(tmp_path: Path):
    plain = tmp_path / "plain"
    plain.mkdir()
    GitSnapshotter(plain).restore("deadbeef")  # must not raise
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest tests/test_snapshot.py -k restore -q --no-cov`
Expected: FAIL — `AttributeError: 'GitSnapshotter' object has no attribute 'restore'`.

- [ ] **Step 3: Write minimal implementation**

Append the methods to `GitSnapshotter` in `src/marim_harness/workspace/snapshot.py`:

```python
    def _tree_files(self, repo: Path, commit: str) -> set[str]:
        out = self._run(repo, "ls-tree", "-r", "--name-only", commit)
        return set(out.splitlines()) if out else set()

    def _present_files(self, repo: Path) -> set[str]:
        tracked = self._run(repo, "ls-files")
        untracked = self._run(repo, "ls-files", "--others", "--exclude-standard")
        files = set()
        for blob in (tracked, untracked):
            if blob:
                files.update(blob.splitlines())
        return files

    def restore(self, commit: str) -> None:
        repo = self._repo()
        if repo is None:
            return
        try:
            # 1. Safety net: snapshot the current state so the rewind is undoable.
            self.capture("refs/marim/checkpoints/_pre_restore", "pre-restore safety snapshot")
            # 2. Remove files that exist now but not in the target snapshot
            #    (created after the checkpoint). Scoped to the diff — never a
            #    blanket clean.
            target = self._tree_files(repo, commit)
            for rel in self._present_files(repo) - target:
                try:
                    (repo / rel).unlink()
                except OSError:
                    pass
            # 3. Restore tracked + untracked content via a throwaway index, so
            #    the user's real index/HEAD are untouched.
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                self._run(repo, "read-tree", commit, env=env)
                self._run(repo, "checkout-index", "-a", "-f", env=env)
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint restore failed: %s", exc.stderr or exc)
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest tests/test_snapshot.py -q --no-cov`
Expected: PASS (all capture + restore tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/workspace/snapshot.py tests/test_snapshot.py
git commit -m "feat(snapshot): GitSnapshotter.restore reverts the working tree safely

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 7: GitSnapshotter.delete + wire into the Harness

**Files:**
- Modify: `src/marim_harness/workspace/snapshot.py` (add `delete`)
- Modify: `src/marim_harness/agent.py` (inject `GitSnapshotter`)
- Test: `tests/test_snapshot.py`, `tests/test_agent_checkpoints.py`

**Interfaces:**
- Consumes: `GitSnapshotter` (Tasks 5-6), `CheckpointManager` constructor's `snapshotter` parameter (Task 2).
- Produces: `GitSnapshotter.delete(self, ref: str) -> None`; `Harness.checkpoints` now constructed with a `GitSnapshotter`, so checkpoints capture and restore files end-to-end.

- [ ] **Step 1: Write the failing tests**

```python
# tests/test_snapshot.py  (append)
def test_delete_removes_ref(tmp_path: Path):
    repo = _init_repo(tmp_path)
    snap = GitSnapshotter(repo)
    snap.capture("refs/marim/checkpoints/s/0", "cp 0")
    snap.delete("refs/marim/checkpoints/s/0")
    result = subprocess.run(
        ["git", "rev-parse", "refs/marim/checkpoints/s/0"],
        cwd=repo, capture_output=True, text=True,
    )
    assert result.returncode != 0  # ref is gone
```

```python
# tests/test_agent_checkpoints.py  (append)
import subprocess


async def test_rewind_restores_workspace_files(tmp_path):
    # GitSnapshotter needs a git repo, so init tmp_path as the workspace. The
    # checkpoint captures the working tree at the START of the turn, so the
    # before/after mutation around run_turn — not the model — is what exercises
    # restore. Unit-level file-restore coverage lives in tests/test_snapshot.py.
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.email", "t@t"], cwd=tmp_path, check=True)
    subprocess.run(["git", "config", "user.name", "t"], cwd=tmp_path, check=True)
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_text_model(), deps)

    (tmp_path / "sentinel.txt").write_text("before\n")
    await harness.run_turn("first turn")               # snapshot captures "before"
    (tmp_path / "sentinel.txt").write_text("after the turn\n")
    harness.checkpoints.rewind(0)                       # back to before the turn
    assert (tmp_path / "sentinel.txt").read_text() == "before\n"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `uv run pytest tests/test_snapshot.py -k delete tests/test_agent_checkpoints.py -k restores_workspace -q --no-cov`
Expected: FAIL — missing `delete`; rewind doesn't restore files (Null snapshotter still wired).

- [ ] **Step 3: Write minimal implementation**

Add `delete` to `GitSnapshotter`:

```python
    def delete(self, ref: str) -> None:
        repo = self._repo()
        if repo is None:
            return
        # Best-effort: deleting an already-absent ref is fine.
        subprocess.run(
            ["git", "update-ref", "-d", ref], cwd=repo,
            capture_output=True, text=True,
        )
```

In `src/marim_harness/agent.py`, import and inject the snapshotter:

```python
from .workspace.snapshot import GitSnapshotter
```

Change the construction added in Task 3 from:

```python
        self.checkpoints = CheckpointManager(self.session)
```

to:

```python
        self.checkpoints = CheckpointManager(
            self.session, GitSnapshotter(deps.workspace_root)
        )
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `uv run pytest tests/test_snapshot.py tests/test_agent_checkpoints.py -q --no-cov`
Expected: PASS.

- [ ] **Step 5: Run gates**

Run: `uv run ruff check src tests && uv run pyright src`
Expected: clean / 0 errors.

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/workspace/snapshot.py src/marim_harness/agent.py tests/test_snapshot.py tests/test_agent_checkpoints.py
git commit -m "feat(checkpoints): wire GitSnapshotter so rewind restores files

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

### Task 8: Docs + full-suite verification

**Files:**
- Modify: `README.md`
- Test: full suite + gates

- [ ] **Step 1: Add a README section**

Under the feature list / a new "Checkpoints & rewind" subsection, add:

```markdown
### Checkpoints & rewind

The harness captures a **checkpoint** at the start of every turn — a record of
the conversation length and, when the workspace is a git repository, a shadow
snapshot of the working tree. Rewind with:

```
/rewind            # list checkpoints for this session
/rewind 3          # restore the conversation and files to before turn #3
```

Rewinding truncates the conversation to that point and, in a git workspace,
restores tracked and untracked files to their snapshot — files created after the
checkpoint are removed. The pre-rewind state is itself saved to
`refs/marim/checkpoints/_pre_restore`, so a rewind is recoverable.

Boundaries:
- Snapshots honor `.gitignore`: ignored files (build output, `.env`, …) are not
  captured or restored.
- Outside a git repository, rewind restores the **conversation only**.
- Your branch, staged index, and HEAD are never modified — only working-tree
  files, and only on restore.
```

- [ ] **Step 2: Run the full suite**

Run: `uv run pytest --no-cov -p no:warnings`
Expected: all green (existing count + the new tests), 0 failures.

- [ ] **Step 3: Run lint + types**

Run: `uv run ruff check . && uv run pyright src`
Expected: "All checks passed!" / "0 errors".

- [ ] **Step 4: Commit**

```bash
git add README.md
git commit -m "docs: document checkpoints & rewind

Co-Authored-By: Claude Opus 4.8 (1M context) <noreply@anthropic.com>
Claude-Session: https://claude.ai/code/session_01FFnw4magf3LE5XyPmBbZtD"
```

---

## Out of scope (explicit follow-ups)

- **Session-delete GC.** This plan prunes refs on the per-session `limit` and on `clear()` (session reset). Deleting a session via `SessionManager.delete` does not yet remove that session's `refs/marim/checkpoints/<id>/*`. Track as a follow-up (it needs the workspace root threaded into the delete path).
- **Staged-index fidelity.** After a file restore, the user's git *staging area* is left as-is; it may reference content that no longer matches the working tree. Acceptable for v1 (re-stage as needed); document if it confuses users.
- **`Esc-Esc` rewind affordance.** This plan ships the `/rewind` command; a keybinding/picker UI is a separate ergonomic task.
- **True mid-run steering** is a different feature (#3 in the design discussion), not part of checkpointing.

---

## Self-Review

**1. Spec coverage** (against the design sketch for #1):
- Conversation rewind via history truncation → Tasks 2-4. ✓
- Shadow-ref file snapshots without touching branch/index/HEAD → Tasks 5, 7 (`capture`, `update-ref`, throwaway index; `test_capture_does_not_touch_user_branch_or_index`). ✓
- Snapshot at the start-of-turn boundary → Task 3 (`run_turn` after `_maybe_compact`). ✓
- Restore handling modify/delete/create-after, scoped (no blanket `git clean`) → Task 6. ✓
- Safety stash before restore → Task 6 (`_pre_restore` ref + test). ✓
- Non-git graceful degrade (conversation-only) → `NullSnapshotter` default (Phase A) + `repo_root` guard returning `None` (Tasks 5-6 `*_outside_git` / `*_noop_outside_git` tests). ✓
- Storage cap / pruning → Task 2 (`_prune`, `test_limit_prunes_oldest`) + `delete` (Task 7). ✓
- `.gitignore` boundary documented → Task 8. ✓
- Sidecar persistence, fail-safe on corrupt file → Task 2 (`reload` swallows `JSONDecodeError`). ✓

**2. Placeholder scan:** every code step contains complete code; commands have expected output. The conftest helpers were verified during planning: `_make_harness(model, deps) -> Harness` and `_text_model() -> FunctionModel` are plain helper functions (not fixtures), and `Deps(workspace_root=tmp_path, mode=Mode.auto)` is the construction used by the existing `tests/test_agent.py:85`. Tasks 3 and 7 use that exact pattern. No placeholders remain.

**3. Type consistency:** `Checkpoint.commit` (Optional[str]) is the snapshot identifier throughout — produced by `Snapshotter.capture`, consumed by `Snapshotter.restore`, stored by `CheckpointManager`. `CheckpointManager.snapshot(prompt_preview: str)`, `.rewind(index: int) -> RewindResult`, `.reload()`, `.clear()`, `.list()` names match across Tasks 2-4 and the Harness wiring. `GitSnapshotter.capture/restore/delete` signatures match the `Snapshotter` Protocol in Task 1. `SessionView.render_session(note: str)` and `app.harness.checkpoints` match the real code read during planning.
