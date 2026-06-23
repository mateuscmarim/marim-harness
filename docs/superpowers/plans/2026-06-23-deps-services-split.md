# Deps/Services Split Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the four Harness-wired collaborator handles off the flat `Deps` bag into a dedicated `HarnessServices` container, so the post-construction wiring is a single cohesive assignment instead of four scattered attribute mutations.

**Architecture:** `Deps` currently mixes two kinds of fields: stable inputs the caller provides (`workspace_root`, `mode`, `tasks`, `jobs`, `command_policy`, `hooks`, `notifier`, `request_approval`, `ask_user`, `on_subagent_event`) and collaborator handles that `Harness.__init__` patches in *after* construction (`lsp`, `turn_hooks`, `run_subagent`, `run_background_agent`). The latter four form a genuine reference cycle (`TurnHooks`/`SubagentRunner` hold `deps`; `deps` must expose them to tools via `ctx.deps`). The cycle makes at least one late binding unavoidable — but today it's four separate `self.deps.X = ...` pokes against always-`None`-then-patched fields intermixed with real inputs. This plan collapses those four into one `HarnessServices` dataclass, assigned once. Tools read `ctx.deps.services.lsp` instead of `ctx.deps.lsp`.

**Tech Stack:** Python 3, dataclasses, pydantic-ai (`RunContext[Deps]`), pytest (`pytest-anyio`), `uv`.

## Global Constraints

- Run tests with `uv run pytest` (the project uses `uv` for envs).
- The main agent's `deps_type` stays `Deps` — do **not** change what pydantic-ai is handed; only restructure `Deps`'s internals.
- Tools reach collaborators exclusively through `ctx.deps` (a `RunContext[Deps]`). Any field that tools read must remain reachable from a `Deps` instance.
- `HarnessServices` fields are all `Optional` and default to `None`: headless runs, subagent tool-name probes, and ~200 test call sites build `Deps(workspace_root=...)` with no services, and that must keep working (every tool already guards with an `is None` check).
- Out of scope (explicit follow-up, not this plan): the TUI-wired callbacks `request_approval`, `ask_user`, `on_subagent_event` (mutated in `interfaces/tui/app.py:86-90`) and the `hooks`/`notifier` caller inputs stay flat on `Deps`. They are interface-layer callbacks, not the Harness-wired cycle, and moving them is a separate axis.

---

### Task 1: Introduce `HarnessServices` and migrate all readers

Define the container, move the four fields off `Deps`, and update every production reader (`provider.py`), the Harness wiring (`agent.py`), and the handful of tests that pass the moved fields as kwargs. This lands atomically — the four fields cannot exist in two places at once, so readers and constructors move together to keep the suite green.

**Files:**
- Modify: `src/marim_harness/deps.py:47-82` (the `Deps` dataclass + add `HarnessServices`)
- Modify: `src/marim_harness/agent.py:276-326` (collapse four mutations into one assignment) and its import block near `:25`
- Modify: `src/marim_harness/tools/provider.py` (reads at lines 89,91,98,100,107,109,115,117,124,126,133,135,220,250,372,377,382,384,407,410)
- Modify: `tests/test_deps.py:19-28` (rewrite the two lsp tests for the new shape)
- Modify: `tests/test_lsp_tools.py` (10 `Deps(..., lsp=...)` sites: 46,54,61,105,116,127,137,149,161,172 + import)
- Modify: `tests/test_jobs_tools.py:101` (one `run_background_agent=` site + import)

**Interfaces:**
- Produces: `HarnessServices` dataclass in `marim_harness.deps` with fields `lsp: Optional[LspManager] = None`, `turn_hooks: Optional[TurnHooks] = None`, `run_subagent: Optional[SubAgentRunner] = None`, `run_background_agent: Optional[BackgroundAgentRunner] = None`.
- Produces: `Deps.services: HarnessServices` field (default `field(default_factory=HarnessServices)`); the four named fields are **removed** from `Deps`.
- Consumes (unchanged): the existing `SubAgentRunner` / `BackgroundAgentRunner` type aliases and the `TYPE_CHECKING` import of `LspManager`/`TurnHooks` already in `deps.py`.

- [ ] **Step 1: Write the failing test for the new structure**

Replace the two existing lsp tests in `tests/test_deps.py` (lines 19-28) with tests for the container shape:

```python
def test_deps_has_services_container_defaulting_to_none():
    from marim_harness.deps import HarnessServices

    d = Deps(workspace_root=Path("."))
    assert isinstance(d.services, HarnessServices)
    assert d.services.lsp is None
    assert d.services.turn_hooks is None
    assert d.services.run_subagent is None
    assert d.services.run_background_agent is None


def test_each_deps_gets_its_own_services_container():
    a = Deps(workspace_root=Path("."))
    b = Deps(workspace_root=Path("."))
    assert a.services is not b.services


def test_lsp_handle_lives_on_services():
    from marim_harness.deps import HarnessServices

    sentinel = object()
    d = Deps(workspace_root=Path("."), services=HarnessServices(lsp=sentinel))
    assert d.services.lsp is sentinel
    # The flat field is gone — accessing it is an attribute error.
    assert not hasattr(d, "lsp")
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_deps.py -v`
Expected: FAIL — `ImportError: cannot import name 'HarnessServices'` (and `Deps` still has a flat `.lsp`).

- [ ] **Step 3: Add `HarnessServices` and rework `Deps` in `deps.py`**

In `src/marim_harness/deps.py`, add the container immediately before the `@dataclass`/`class Deps` block (it must be defined before `Deps` references it):

```python
@dataclass
class HarnessServices:
    """Collaborator handles wired by the Harness after construction.

    These four form a reference cycle with ``Deps``: ``TurnHooks`` and the
    sub-agent runners hold the ``deps`` object, while tools reach them back
    through ``ctx.deps.services``. The cycle makes one late binding
    unavoidable — the Harness builds these, then assigns the populated
    container onto ``deps.services`` in a single step (see ``agent.py``).
    Every field is optional: headless runs and tests leave them ``None`` and
    each tool guards with an ``is None`` check.
    """

    # Session-scoped LSP server pool. None when LSP is disabled.
    lsp: Optional["LspManager"] = None
    # Session-bound hook dispatcher, so tools (ask_user, update_tasks) can fire
    # lifecycle hooks with a full payload. None when no hooks are configured.
    turn_hooks: Optional["TurnHooks"] = None
    # Lets the spawn_agent tool launch a sub-agent and stream its events.
    run_subagent: Optional[SubAgentRunner] = None
    # Lets spawn_agent(background=True) run a sub-agent as a detached job.
    run_background_agent: Optional[BackgroundAgentRunner] = None
```

Then in the `Deps` dataclass, **delete** these four fields (current lines 66-74 / 79-82): `lsp`, `run_subagent`, `run_background_agent`, `turn_hooks`. Keep `hooks`, `notifier`, `on_subagent_event` where they are. Add the container field — place it right after `command_policy` (line 63):

```python
    # Collaborator handles wired by the Harness after construction. Its own
    # container so the late-bound services are separated from caller inputs.
    services: HarnessServices = field(default_factory=HarnessServices)
```

After editing, `Deps` should still carry: `workspace_root`, `mode`, `request_approval`, `ask_user`, `tasks`, `jobs`, `command_policy`, `services`, `hooks`, `on_subagent_event`, `notifier`.

- [ ] **Step 4: Migrate the production readers in `provider.py`**

In `src/marim_harness/tools/provider.py`, replace every `ctx.deps.lsp` with `ctx.deps.services.lsp` (lines 89, 91, 98, 100, 107, 109, 115, 117, 124, 126, 133, 135, 407, 410), every `ctx.deps.run_background_agent` with `ctx.deps.services.run_background_agent` (lines 372, 377), and `ctx.deps.run_subagent` with `ctx.deps.services.run_subagent` (lines 382, 384).

For the two `turn_hooks` reads (lines 220, 250), the current code is defensive:

```python
    th = getattr(ctx.deps, "turn_hooks", None)
```

`services` is always present now, so read it directly:

```python
    th = ctx.deps.services.turn_hooks
```

- [ ] **Step 5: Collapse the Harness wiring into one assignment**

In `src/marim_harness/agent.py`, add `HarnessServices` to the deps import (near line where `Deps` is imported — find `from .deps import` and add `HarnessServices`; if `Deps` is imported as `from .deps import Deps`, change to `from .deps import Deps, HarnessServices`).

Delete the four scattered mutation lines:
- line 279: `self.deps.lsp = self.lsp`
- line 308: `self.deps.turn_hooks = self.hooks`
- line 325: `self.deps.run_subagent = self.subagents.run`
- line 326: `self.deps.run_background_agent = self.subagents.run_background`

(Keep `self.lsp = ...` on line 278, `self.hooks = TurnHooks(...)` on 306, and the `self.subagents = SubagentRunner(...)` block — `Harness` still owns those attributes; `aclose` and `run_turn` use them.)

Then, immediately after the `self.subagents = SubagentRunner(...)` block closes (where line 326 used to be), assemble the container once:

```python
        # One cohesive late binding for the collaborator cycle: TurnHooks and
        # the sub-agent runners hold this deps object, and tools reach them
        # back through ctx.deps.services. Assigned here as a finished container
        # rather than four scattered self.deps.X = ... pokes.
        self.deps.services = HarnessServices(
            lsp=self.lsp,
            turn_hooks=self.hooks,
            run_subagent=self.subagents.run,
            run_background_agent=self.subagents.run_background,
        )
```

- [ ] **Step 6: Migrate the tests that pass the moved fields as kwargs**

In `tests/test_lsp_tools.py`, add `HarnessServices` to the import:

```python
from marim_harness.deps import Deps, HarnessServices
```

Replace each `Deps(workspace_root=tmp_path, lsp=X)` with `Deps(workspace_root=tmp_path, services=HarnessServices(lsp=X))` at lines 46, 54, 61, 105, 116, 127, 137, 149, 161, 172. For example, line 46:

```python
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=lsp)))
```

and the `lsp=None` case (line 54):

```python
    ctx = _Ctx(Deps(workspace_root=tmp_path, services=HarnessServices(lsp=None)))
```

In `tests/test_jobs_tools.py`, add `HarnessServices` to the `from marim_harness.deps import ...` line, then change line 101:

```python
    deps = Deps(workspace_root=tmp_path,
                services=HarnessServices(run_background_agent=fake_bg))
```

- [ ] **Step 7: Run the full suite and verify green**

Run: `uv run pytest -q`
Expected: PASS. If any failure mentions `AttributeError: 'Deps' object has no attribute 'lsp'` (or `run_subagent`/`run_background_agent`/`turn_hooks`), that reader was missed — grep and fix:

Run: `grep -rnE "deps\.(lsp|turn_hooks|run_subagent|run_background_agent)\b" src/ tests/ | grep -v "services\."`
Expected: no output (every access now goes through `.services.`).

- [ ] **Step 8: Commit**

```bash
git add src/marim_harness/deps.py src/marim_harness/agent.py src/marim_harness/tools/provider.py tests/test_deps.py tests/test_lsp_tools.py tests/test_jobs_tools.py
git commit -m "refactor(deps): move Harness-wired handles into HarnessServices container"
```

---

### Task 2: Extract a `build_services()` factory

Pull the collaborator construction + container assembly out of the `Harness.__init__` body into a named, unit-testable function. This shrinks the constructor and makes the late-binding behaviour assertable without standing up a full `Harness`.

**Files:**
- Modify: `src/marim_harness/agent.py` (the construction block in `__init__`, ~lines 276-326 after Task 1)
- Test: `tests/test_deps.py` (add a `build_services` unit test) — or `tests/test_agent.py` if the implementer prefers agent-adjacent placement; this plan uses `test_deps.py`.

**Interfaces:**
- Consumes: `HarnessServices` (from Task 1), `LspManager`, `TurnHooks`, `SubagentRunner`.
- Produces: a module-level function in `marim_harness.agent`:

```python
def build_services(
    deps: Deps,
    *,
    lsp: Optional[LspManager],
    turn_hooks: TurnHooks,
    subagents: SubagentRunner,
) -> HarnessServices:
    ...
```

  It assembles and returns a populated `HarnessServices` **and** assigns it onto `deps.services`, returning the same container the caller can ignore or keep. (The assignment stays here because the cycle requires it; the win is that it now lives in one named, tested place.)

- [ ] **Step 1: Write the failing test**

Add to `tests/test_deps.py`:

```python
def test_build_services_populates_and_assigns(tmp_path):
    from marim_harness.agent import build_services
    from marim_harness.deps import Deps, HarnessServices

    deps = Deps(workspace_root=tmp_path)
    lsp = object()
    turn_hooks = object()

    class _Subs:
        async def run(self, *a, **k): ...
        async def run_background(self, *a, **k): ...

    subs = _Subs()
    services = build_services(deps, lsp=lsp, turn_hooks=turn_hooks, subagents=subs)

    assert isinstance(services, HarnessServices)
    assert services.lsp is lsp
    assert services.turn_hooks is turn_hooks
    assert services.run_subagent is subs.run
    assert services.run_background_agent is subs.run_background
    # The container is also installed on deps (the late binding).
    assert deps.services is services
```

- [ ] **Step 2: Run the test to verify it fails**

Run: `uv run pytest tests/test_deps.py::test_build_services_populates_and_assigns -v`
Expected: FAIL — `ImportError: cannot import name 'build_services' from 'marim_harness.agent'`.

- [ ] **Step 3: Add the factory and call it from `__init__`**

In `src/marim_harness/agent.py`, add the module-level function (place it above `class Harness`):

```python
def build_services(
    deps: Deps,
    *,
    lsp: Optional[LspManager],
    turn_hooks: TurnHooks,
    subagents: SubagentRunner,
) -> HarnessServices:
    """Assemble the Harness-wired collaborator container and install it on
    ``deps``. Centralises the one late binding the deps<->services cycle
    requires (see HarnessServices)."""
    services = HarnessServices(
        lsp=lsp,
        turn_hooks=turn_hooks,
        run_subagent=subagents.run,
        run_background_agent=subagents.run_background,
    )
    deps.services = services
    return services
```

Then in `Harness.__init__`, replace the inline `self.deps.services = HarnessServices(...)` block added in Task 1 with a call to the factory:

```python
        build_services(
            self.deps,
            lsp=self.lsp,
            turn_hooks=self.hooks,
            subagents=self.subagents,
        )
```

- [ ] **Step 4: Run the tests to verify they pass**

Run: `uv run pytest tests/test_deps.py -v`
Expected: PASS.

- [ ] **Step 5: Run the full suite**

Run: `uv run pytest -q`
Expected: PASS (the `Harness` construction path is exercised by the existing `test_agent*.py` suites).

- [ ] **Step 6: Commit**

```bash
git add src/marim_harness/agent.py tests/test_deps.py
git commit -m "refactor(agent): extract build_services factory for the deps<->services binding"
```

---

## Self-Review

**Spec coverage (against the P0 items the user asked to plan):**
- "Split `Deps` into config vs wired services" → Task 1 moves the four Harness-wired handles into `HarnessServices`, leaving caller inputs flat. ✔
- "Stop `Harness.__init__` mutating `deps` (four scattered pokes)" → Task 1 collapses four mutations into one; Task 2 names and isolates that single binding in `build_services`. The cycle makes total elimination impossible, and the plan says so explicitly in the Architecture note and the `HarnessServices` docstring. ✔
- TUI callbacks (`request_approval`/`ask_user`/`on_subagent_event`) — deliberately deferred and called out under Global Constraints as out of scope. ✔

**Placeholder scan:** No "TBD"/"handle edge cases"/"similar to" — every code step shows the actual code; the mechanical `provider.py` edits enumerate exact line numbers and a verification grep backstops them.

**Type consistency:** `HarnessServices` fields (`lsp`, `turn_hooks`, `run_subagent`, `run_background_agent`) are named identically in `deps.py` (Task 1 Step 3), the `agent.py` assembly (Task 1 Step 5), the `build_services` signature/body (Task 2 Step 3), and all three test files. `Deps.services` is referenced consistently as `ctx.deps.services.<field>` in `provider.py` and `deps.services` in the factory.
