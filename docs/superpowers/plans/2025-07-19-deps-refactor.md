# Deps Refactor Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Decompose the 15-field `Deps` dataclass into typed sub-dataclasses (`WorkspaceConfig`, `UIHooks`), reducing it to 6 fields and separating UI callbacks from core runtime state.

**Architecture:** Three new/modified dataclasses in `runtime/deps.py`. All call sites updated mechanically (field-path changes only, no logic changes). Test construction sites use a new `_make_deps` helper for brevity.

**Tech Stack:** Python 3.10+, Pydantic AI, Pyright (standard mode), Ruff (E,F,I,UP,B,SIM)

## Global Constraints

- Python >=3.10 (no 3.11+ syntax)
- Ruff line length 100
- Pyright typeCheckingMode = "standard"
- `from __future__ import annotations` in all files
- No `# type: ignore` added
- All imports under `TYPE_CHECKING` where possible to avoid runtime cost
- Commit after each task

---

## Task 1: Restructure `runtime/deps.py`

**Files:**
- Modify: `src/marim_harness/runtime/deps.py`

**What changes:**
- Add `WorkspaceConfig` dataclass (fields: `root: Path`, `mode: Mode`, `command_policy: CommandPolicy`)
- Add `UIHooks` dataclass (fields: `request_approval`, `ask_user`, 4 sub-agent callbacks, `detach_fanout`, `interactive`, `notifier`)
- Restructure `Deps` to use `workspace: WorkspaceConfig` and `ui: UIHooks`
- Remove `SubAgentCallbacks` class
- Keep all callback type aliases (`ApprovalFn`, `AskUserFn`, etc.) — they're still used
- `HarnessServices`, `SubAgentRunner`, `BackgroundAgentRunner` type aliases stay

- [ ] **Step 1: Add WorkspaceConfig and UIHooks dataclasses, restructure Deps**

```python
# After the existing type aliases (ApprovalFn, AskUserFn, etc.), before Deps:

@dataclass
class WorkspaceConfig:
    """Immutable workspace identity. Set once at construction, never mutated."""
    root: Path
    mode: Mode = Mode.ask
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)


@dataclass
class UIHooks:
    """UI callbacks wired by bind_ui(). All None when headless.

    SubAgentCallbacks fields are absorbed here — they are UI-layer concerns
    (streaming sub-agent events to the TUI) and don't belong on the core
    runtime object.
    """
    request_approval: ApprovalFn | None = None
    ask_user: AskUserFn | None = None
    on_subagent_event: SubAgentEventCb | None = None
    on_subagent_notice: SubAgentNoticeCb | None = None
    on_subagent_model: SubAgentModelCb | None = None
    on_subagent_usage: SubAgentUsageCb | None = None
    detach_fanout: bool = False
    interactive: bool = False
    notifier: "Notifier | None" = None
```

- [ ] **Step 2: Replace Deps class**

```python
@dataclass
class Deps:
    workspace: WorkspaceConfig
    ui: UIHooks = field(default_factory=UIHooks)
    tasks: TaskList = field(default_factory=TaskList)
    jobs: JobRegistry = field(default_factory=JobRegistry)
    services: HarnessServices = field(default_factory=HarnessServices)
    hooks: "HookRunner | None" = None
```

- [ ] **Step 3: Remove SubAgentCallbacks class** (the `@dataclass class SubAgentCallbacks` block)

- [ ] **Step 4: Remove unused imports if any** (check that all imports are still needed)

- [ ] **Step 5: Verify pyright on deps.py alone**

Run: `uv run pyright src/marim_harness/runtime/deps.py`
Expected: 0 errors (other files will fail until updated)

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/runtime/deps.py
git commit -m "refactor(deps): introduce WorkspaceConfig and UIHooks sub-dataclasses

Replace the 15-field Deps god-object with a 6-field composition root.
WorkspaceConfig holds workspace identity (root, mode, command_policy).
UIHooks holds all UI callbacks (approval, ask_user, sub-agent events,
notifier, detach_fanout, interactive). SubAgentCallbacks removed — its
fields absorbed into UIHooks."
```

---

## Task 2: Update `runtime/` (harness, bootstrap, controller)

**Files:**
- Modify: `src/marim_harness/runtime/harness.py`
- Modify: `src/marim_harness/runtime/bootstrap.py`
- Modify: `src/marim_harness/runtime/controller.py`

**Interfaces:**
- Consumes: `WorkspaceConfig`, `UIHooks`, new `Deps` from Task 1
- Produces: Updated `bind_ui()`, `mode` property, `build_collaborators()`

- [ ] **Step 1: Update `runtime/harness.py`**

Changes needed:
1. Remove `SubAgentCallbacks` from imports (line 53)
2. Update `build_collaborators`: `deps.workspace_root` → `deps.workspace.root` (lines 205, 213)
3. Update `bind_ui`: set `deps.ui.*` fields instead of `deps.*`, replace `SubAgentCallbacks(...)` with direct `deps.ui.on_subagent_*` assignments (lines 331-345)
4. Update `mode` property: `deps.mode` → `deps.workspace.mode` (lines 406, 411, 415, 416)
5. Update `workspace_root` property (if exists): `deps.workspace_root` → `deps.workspace.root`
6. Update `disable_server`/`enable_server`: `deps.workspace_root` → `deps.workspace.root` (lines 438, 441)

- [ ] **Step 2: Update `runtime/bootstrap.py`**

Change the `Deps(...)` construction (line ~53):
```python
# Before:
deps = Deps(
    workspace_root=workspace,
    mode=cfg.default_mode,
    command_policy=cfg.command_policy,
    ...
)

# After:
deps = Deps(
    workspace=WorkspaceConfig(
        root=workspace,
        mode=cfg.default_mode,
        command_policy=cfg.command_policy,
    ),
    ...
)
```
Add `WorkspaceConfig` to imports from `.deps`.

- [ ] **Step 3: Update `runtime/controller.py`**

Changes needed:
1. `deps.mode` → `deps.workspace.mode` (line 522, 539)
2. `deps.request_approval` → `deps.ui.request_approval` (line 539)

- [ ] **Step 4: Verify pyright on runtime/**

Run: `uv run pyright src/marim_harness/runtime/`
Expected: 0 errors

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/harness.py src/marim_harness/runtime/bootstrap.py src/marim_harness/runtime/controller.py
git commit -m "refactor(runtime): update harness, bootstrap, controller for new Deps structure"
```

---

## Task 3: Update `tools/provider.py`

**Files:**
- Modify: `src/marim_harness/tools/provider.py`

**What changes:** All `ctx.deps.*` field paths updated. This is the largest single file — ~30 call sites.

- [ ] **Step 1: Update all field paths**

Mechanical replacements (use find/replace):
1. `ctx.deps.workspace_root` → `ctx.deps.workspace.root` (~15 sites)
2. `ctx.deps.mode` → `ctx.deps.workspace.mode` (check if any — controller handles most mode checks)
3. `ctx.deps.command_policy` → `ctx.deps.workspace.command_policy` (1 site, in `bash()`)
4. `ctx.deps.ask_user` → `ctx.deps.ui.ask_user` (2 sites, in `ask_user()`)
5. `ctx.deps.detach_fanout` → `ctx.deps.ui.detach_fanout` (1 site, in `spawn_agent()`)
6. `ctx.deps.interactive` → `ctx.deps.ui.interactive` (1 site, in `spawn_agent()`)

- [ ] **Step 2: Verify pyright on tools/**

Run: `uv run pyright src/marim_harness/tools/`
Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
git add src/marim_harness/tools/provider.py
git commit -m "refactor(tools): update provider.py for new Deps structure"
```

---

## Task 4: Update `subagents/runner.py`

**Files:**
- Modify: `src/marim_harness/subagents/runner.py`

**What changes:**
1. `deps.mode` → `deps.workspace.mode` (lines 287, 644)
2. `deps.callbacks.on_event` → `deps.ui.on_subagent_event` (line 225)
3. `deps.callbacks.on_notice` → `deps.ui.on_subagent_notice` (line 406)
4. `deps.callbacks` → `deps.ui` (line 648 — accesses multiple sub-agent callbacks)

- [ ] **Step 1: Update all field paths**

- [ ] **Step 2: Verify pyright on subagents/**

Run: `uv run pyright src/marim_harness/subagents/`
Expected: 0 errors

- [ ] **Step 3: Commit**

```bash
git add src/marim_harness/subagents/runner.py
git commit -m "refactor(subagents): update runner.py for new Deps structure"
```

---

## Task 5: Update interfaces (TUI + CLI)

**Files:**
- Modify: `src/marim_harness/interfaces/tui/app.py`
- Modify: `src/marim_harness/interfaces/tui/stream_render.py`
- Modify: `src/marim_harness/interfaces/tui/settings.py`
- Modify: `src/marim_harness/interfaces/tui/status.py`
- Modify: `src/marim_harness/interfaces/cli/headless.py`

- [ ] **Step 1: Update `interfaces/tui/app.py`**

Changes:
1. `harness.deps.workspace_root` → `harness.deps.workspace.root` (line 163)
2. `harness.deps.notifier` → `harness.deps.ui.notifier` (line 309)
3. Add `WorkspaceConfig` to imports from `..runtime.deps` if constructing Deps here

- [ ] **Step 2: Update `interfaces/tui/stream_render.py`**

Changes:
1. `deps.callbacks.on_event` → `deps.ui.on_subagent_event` (check all sites)
2. `deps.callbacks.on_notice` → `deps.ui.on_subagent_notice`
3. `deps.callbacks.on_model` → `deps.ui.on_subagent_model`
4. `deps.callbacks.on_usage` → `deps.ui.on_subagent_usage`

- [ ] **Step 3: Update `interfaces/tui/settings.py`**

Changes:
1. `harness.deps.mode` → `harness.deps.workspace.mode` (lines 164, 178, 327)

- [ ] **Step 4: Update `interfaces/tui/status.py`**

Changes:
1. `app.harness.deps.mode` → `app.harness.deps.workspace.mode` (line 99)

- [ ] **Step 5: Update `interfaces/cli/headless.py`**

Changes:
1. `harness.deps.notifier` → `harness.deps.ui.notifier` (line 51)
2. Update Deps construction if present

- [ ] **Step 6: Verify pyright on interfaces/**

Run: `uv run pyright src/marim_harness/interfaces/`
Expected: 0 errors

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/interfaces/
git commit -m "refactor(interfaces): update TUI and CLI for new Deps structure"
```

---

## Task 6: Update tests

**Files:**
- Modify: `tests/conftest.py` (add `_make_deps` helper)
- Modify: ~30 test files (update `Deps(workspace_root=...)` construction)

**Top files by count (update these first):**
- `test_agent_subagents.py` — 30 sites
- `test_agent_mcp.py` — 24 sites
- `test_agent_sessions.py` — 22 sites
- `test_provider.py` — 19 sites
- `test_agent.py` — 12 sites
- `test_lsp_tools.py` — 11 sites
- `test_jobs_tools.py` — 11 sites
- `test_session.py` — 10 sites
- `test_agent_hooks.py` — 10 sites

- [ ] **Step 1: Add `_make_deps` helper to `tests/conftest.py`**

```python
from pathlib import Path
from marim_harness.runtime.deps import Deps, WorkspaceConfig
from marim_harness.runtime.permissions import Mode

def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    """Shorthand for Deps construction in tests."""
    return Deps(workspace=WorkspaceConfig(root=root, mode=mode), **kw)
```

Add this after the existing `_make_harness` helper (around line 116).

- [ ] **Step 2: Update test files (batch 1 — highest count)**

For each file, replace all `Deps(workspace_root=tmp_path, mode=Mode.auto)` with `_make_deps(tmp_path)` and `Deps(workspace_root=tmp_path)` with `_make_deps(tmp_path, mode=Mode.ask)`.

Use find/replace patterns:
- `Deps(workspace_root=tmp_path, mode=Mode.auto)` → `_make_deps(tmp_path)`
- `Deps(workspace_root=tmp_path, mode=Mode.auto, ` → `_make_deps(tmp_path, ` (with trailing args)
- `Deps(workspace_root=tmp_path)` → `_make_deps(tmp_path, mode=Mode.ask)`
- `Deps(workspace_root=tmp_path, ` → `_make_deps(tmp_path, ` (for other kwargs)

Files: `test_agent_subagents.py`, `test_agent_mcp.py`, `test_agent_sessions.py`, `test_provider.py`, `test_agent.py`, `test_lsp_tools.py`, `test_jobs_tools.py`, `test_session.py`, `test_agent_hooks.py`

- [ ] **Step 3: Update test files (batch 2 — remaining)**

Files: `test_subagent_isolation.py`, `test_tasks_tool.py`, `test_skills_tool.py`, `test_recovery.py`, `test_deps.py`, `test_memory_tool.py`, `test_app.py`, `test_agent_instructions.py`, `test_subagent_safety.py`, `test_subagent_model.py`, `test_subagent_cli_spawn.py`, `test_agent_checkpoints.py`, `test_agent_consumables.py`, `test_detach_fanout.py`, `test_provider_errors.py`, `test_steering.py`, `test_logging.py`, `test_notifications.py`, `test_notifications_async.py`, `test_session_duration.py`, `test_session_persist_cache.py`, `test_session_view_replay.py`, `test_subagent_concurrency.py`, `test_subagent_retry.py`, `test_subagent_timing.py`, `test_subagent_tool.py`, `test_subagent_transcript_capture.py`, `test_turn_controller.py`, `test_image_attachments.py`, `test_image_paste.py`, `test_live_smoke.py`, `test_cli.py`, `test_headless.py`, `test_app_decomposition.py`, `test_app_turn_race.py`, `test_queue.py`, `test_ask_user_tool.py`

Also update any test that imports `SubAgentCallbacks` — replace with `UIHooks` import.

- [ ] **Step 4: Verify pyright on tests/**

Run: `uv run pyright tests/`
Expected: 0 errors

- [ ] **Step 5: Commit**

```bash
git add tests/
git commit -m "refactor(tests): update Deps construction for new sub-dataclass structure

Add _make_deps() helper to conftest.py. Update ~30 test files from
Deps(workspace_root=..., mode=...) to _make_deps(...) for brevity."
```

---

## Task 7: Full verification and cleanup

- [ ] **Step 1: Run ruff**

Run: `uv run ruff check src tests`
Expected: All checks passed

- [ ] **Step 2: Run ruff fix if needed**

Run: `uv run ruff check --fix src tests`
Expected: any import ordering issues fixed automatically

- [ ] **Step 3: Run pyright**

Run: `uv run pyright`
Expected: 0 errors, 0 warnings

- [ ] **Step 4: Run full test suite**

Run: `uv run pytest -x -q`
Expected: All tests pass

- [ ] **Step 5: Verify success criteria**

Check:
- `SubAgentCallbacks` class no longer exists in codebase
- `Deps` has exactly 6 fields
- `WorkspaceConfig` and `UIHooks` are importable from `runtime.deps`
- No `# type: ignore` added during this refactor

Run: `grep -r "SubAgentCallbacks" src/` — should return 0 results
Run: `grep -r "# type: ignore" src/marim_harness/runtime/deps.py` — should return 0 results

- [ ] **Step 6: Final commit (if any cleanup was needed)**

```bash
git add -u
git commit -m "chore: final cleanup for Deps refactor"
```
