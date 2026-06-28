# Deps Refactor: Sub-Dataclass Decomposition

## Problem

`Deps` is a 15-field dataclass that serves as the single context object threaded through every tool via `RunContext[Deps]`. It mixes four concerns:

1. **Workspace identity** — `workspace_root`, `mode`, `command_policy`
2. **UI callbacks** — `request_approval`, `ask_user`, `callbacks` (4 sub-agent callbacks), `notifier`, `detach_fanout`, `interactive`
3. **Mutable state** — `tasks`, `jobs`
4. **Late-bound services** — `services`, `hooks`

This creates two problems:

- **Cognitive load:** 15 flat fields in one class are hard to hold in your head. It's unclear at a glance what a given consumer actually needs.
- **UI/runtime coupling:** UI callbacks (`request_approval`, `ask_user`, `notifier`, sub-agent event callbacks) live on the core runtime object even though they're `None` in headless mode and only wired by the TUI. The dependency flows the wrong way — the core runtime shouldn't know about UI concepts.

## Solution

Decompose `Deps` into three typed sub-dataclasses, each owning one concern. `Deps` becomes a thin composition root with 6 fields.

### New types (all in `runtime/deps.py`)

```python
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


@dataclass
class Deps:
    workspace: WorkspaceConfig
    ui: UIHooks = field(default_factory=UIHooks)
    tasks: TaskList = field(default_factory=TaskList)
    jobs: JobRegistry = field(default_factory=JobRegistry)
    services: HarnessServices = field(default_factory=HarnessServices)
    hooks: "HookRunner | None" = None
```

**15 fields → 6 fields.** `SubAgentCallbacks` is removed as a separate class — its 4 fields move into `UIHooks`.

### What stays flat on Deps

- `tasks` — mutated by tools (`update_tasks`), rendered by TUI. Lightweight, no reason to nest.
- `jobs` — mutated by tools (`bash` background, `spawn_agent` background), rendered by TUI. Same reasoning.
- `services` — already a well-structured sub-object (`HarnessServices`). No change needed.
- `hooks` — nullable, used by tools and runtime. Single field, no grouping needed.

### Call-site migration

| Before | After | Count |
|--------|-------|-------|
| `deps.workspace_root` | `deps.workspace.root` | ~30 |
| `deps.mode` | `deps.workspace.mode` | ~10 |
| `deps.command_policy` | `deps.workspace.command_policy` | 1 |
| `deps.request_approval` | `deps.ui.request_approval` | 3 |
| `deps.ask_user` | `deps.ui.ask_user` | 2 |
| `deps.detach_fanout` | `deps.ui.detach_fanout` | 2 |
| `deps.interactive` | `deps.ui.interactive` | 2 |
| `deps.notifier` | `deps.ui.notifier` | 2 |
| `deps.callbacks.on_event` | `deps.ui.on_subagent_event` | 6 |
| `deps.callbacks.on_notice` | `deps.ui.on_subagent_notice` | 1 |
| `deps.callbacks.on_model` | `deps.ui.on_subagent_model` | 1 |
| `deps.callbacks.on_usage` | `deps.ui.on_subagent_usage` | 1 |
| `deps.tasks` | `deps.tasks` | no change |
| `deps.jobs` | `deps.jobs` | no change |
| `deps.services.*` | `deps.services.*` | no change |
| `deps.hooks` | `deps.hooks` | no change |

All changes are mechanical field-path updates. No logic changes.

### Files affected

**Production (~7 files):**
- `runtime/deps.py` — define `WorkspaceConfig`, `UIHooks`, restructure `Deps`, remove `SubAgentCallbacks`
- `runtime/bootstrap.py` — update `Deps()` construction
- `runtime/harness.py` — update `bind_ui()` to set `deps.ui.*`, update `mode` property, remove `SubAgentCallbacks` import
- `runtime/controller.py` — update `deps.workspace.mode` and `deps.ui.request_approval` access (note: `resolve_approvals` in `permissions.py` receives mode and approval fn as params from controller, so no changes needed in permissions.py itself)
- `tools/provider.py` — update all `ctx.deps.workspace_root` → `ctx.deps.workspace.root`, `ctx.deps.mode` → `ctx.deps.workspace.mode`, UI field paths
- `interfaces/cli/headless.py` — update `deps.ui.notifier` access, construction
- `subagents/runner.py` — update `deps.workspace.mode` access

**TUI (~5 files):**
- `interfaces/tui/app.py` — update `deps.notifier` → `deps.ui.notifier`, construction
- `interfaces/tui/stream_render.py` — update `deps.callbacks.on_*` → `deps.ui.on_subagent_*`
- `interfaces/tui/settings.py` — update `deps.mode` → `deps.workspace.mode`
- `interfaces/tui/status.py` — update `deps.mode` → `deps.workspace.mode`

**Tests (~30 files):**
- Every `Deps(workspace_root=tmp_path, mode=Mode.auto)` becomes `Deps(workspace=WorkspaceConfig(root=tmp_path, mode=Mode.auto))`
- A `_make_deps()` helper in `conftest.py` reduces verbosity

### Construction helper (conftest.py)

```python
def _make_deps(root: Path, mode: Mode = Mode.auto, **kw) -> Deps:
    """Shorthand for Deps construction in tests."""
    return Deps(workspace=WorkspaceConfig(root=root, mode=mode), **kw)
```

### bind_ui migration

```python
# Before
def bind_ui(self, *, request_approval=None, ask_user=None, ...):
    self.deps.request_approval = request_approval
    self.deps.ask_user = ask_user
    self.deps.callbacks.on_event = on_subagent_event
    ...
    self.deps.interactive = True

# After
def bind_ui(self, *, request_approval=None, ask_user=None, ...):
    self.deps.ui.request_approval = request_approval
    self.deps.ui.ask_user = ask_user
    self.deps.ui.on_subagent_event = on_subagent_event
    ...
    self.deps.ui.interactive = True
```

### mode property on Harness

```python
# Before
@property
def mode(self) -> Mode:
    return self.deps.mode

@mode.setter
def mode(self, mode: Mode) -> None:
    self.deps.mode = mode

# After
@property
def mode(self) -> Mode:
    return self.deps.workspace.mode

@mode.setter
def mode(self, mode: Mode) -> None:
    self.deps.workspace.mode = mode
```

## What this does NOT change

- `RunContext[Deps]` — tools still receive `ctx: RunContext[Deps]`. No generic parameter changes.
- `HarnessServices` — already a clean sub-object with clear purpose.
- `TaskList`, `JobRegistry` — stay flat on Deps (mutated by tools, rendered by TUI, lightweight).
- `HookRunner` — stays flat (nullable, used by tools and runtime, no grouping benefit).
- No logic changes anywhere — this is a pure structural refactor.
- No behavioral changes — all tests should pass without assertion changes.

## Invariants preserved

1. **Headless mode:** `deps.ui.*` fields are all `None`. Tools guard with `is None` checks — no change needed.
2. **Late binding cycle:** `Deps.services` ↔ `TurnHooks`/`SubagentRunner` cycle is unchanged. `build_services()` still performs the single binding step.
3. **Tool registration:** `BuiltinToolProvider` and sub-agent tool registration are unchanged — they depend on tool names, not Deps structure.
4. **Session persistence:** `Deps` is not persisted — only `TaskList` items are serialized. No format changes.

## Success criteria

- `uv run pyright` — 0 errors
- `uv run ruff check src tests` — clean
- `uv run pytest` — all tests pass (90%+ coverage maintained)
- No `# type: ignore` added
- `SubAgentCallbacks` class removed
- `Deps` has exactly 6 fields
- `WorkspaceConfig` and `UIHooks` are importable from `runtime.deps`

## Out of scope

- Changing `HarnessServices` internals
- Adding new capabilities to `Deps`
- Refactoring `resolve_approvals` or `permissions.py` beyond field-path updates
- Adding tests for the refactor itself (no behavior changes to test)
