# LSP Toolset Deferral Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Move the main agent's six LSP navigation tools from static `agent.tool()` registration to a `FunctionToolset` routed through the per-turn tool-search deferral path, sharing one `should_defer` budget with the MCP surface.

**Architecture:** A new `build_lsp_toolset()` wraps the existing six `lsp_tools.py` functions. A new `compose_turn_toolsets()` folds the live MCP toolsets and the LSP toolset into the per-turn list under one unified deferral decision. `provider.register()` stops statically registering LSP on the main agent; the controller calls `compose_turn_toolsets` instead of `mcp.toolsets_for`; the Harness injects the toolset via a new `provider.lsp_toolset()`. Sub-agents are untouched.

**Tech Stack:** Python ≥3.10, Pydantic AI 1.107.0 (`FunctionToolset`, `CombinedToolset`, `DeferredLoadingToolset`, `AbstractToolset`), pytest + anyio.

## Global Constraints

- Python `>=3.10`; no 3.11+-only syntax. Use `from __future__ import annotations` where `X | Y` unions appear at runtime.
- Ruff line length 100; lint set `E,F,I,UP,B,SIM` (import sorting enforced). Run `uv run ruff check src tests`.
- Type-check clean under `uv run pyright` (standard mode, src only).
- Use `uv` for everything (`uv run pytest`, never bare `python`/`pytest`/`pip`).
- Tests never hit the network or real MCP servers — the MCP manager is faked/stubbed.
- Match the CI order locally before claiming done: `uv run ruff check src tests` → `uv run pyright` → `uv run pytest`.
- The shared `anyio_backend` fixture already lives in `tests/conftest.py` (returns `"asyncio"`); async tests use `@pytest.mark.anyio` and must NOT redefine it locally.
- Behavior-preservation invariant: when LSP tools are disabled (`lsp_toolset=None`), `compose_turn_toolsets` must reproduce today's `mcp.toolsets_for` output exactly.
- Commit footer on every commit:
  ```
  Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
  Claude-Session: https://claude.ai/code/session_017n1ei7XPReCcBAMwHJQHin
  ```

## Verified facts (from a spike + code reading — do not re-investigate)

- `CombinedToolset([FunctionToolset(...)])` and `DeferredLoadingToolset(CombinedToolset(...))` both build; `FunctionToolset` is an `AbstractToolset`. (Open item resolved — no fallback needed.)
- `should_defer(policy, count, threshold)` (`mcp/manager.py:14`): `"on"`→always defer; `"auto"`→`count > threshold` (strict); else→never.
- `McpManager.live_toolsets() -> list` (sync); `McpManager.live_tool_count() -> int` (async).
- Controller per-turn call is `runtime/controller.py:858`: `toolsets = await self.mcp.toolsets_for(self.deps.workspace.tool_search, self.deps.workspace.tool_search_threshold)`.
- `TurnController` is constructed in `Harness.__init__` (`runtime/harness.py:362`) with kwargs `agent, session, checkpoints, hooks, mcp, deps, get_model`. `self.provider` (the `BuiltinToolProvider`) is in scope there.
- The provider's `_register_lsp_tools` flag = `cfg.lsp_enabled and cfg.lsp_tools_enabled` (derived in `bootstrap.py:109`, passed to `BuiltinToolProvider(register_lsp_tools=…)`).
- `LSP_TOOLS` (frozenset of the six names) is in `tools/names.py`; `len(LSP_TOOLS) == 6`.

## File Structure

- Modify `src/marim_harness/tools/lsp_tools.py` — add `build_lsp_toolset()`.
- Create `src/marim_harness/runtime/toolsets.py` — `compose_turn_toolsets()`.
- Modify `src/marim_harness/tools/provider.py` — drop LSP block from `register()`; add `lsp_toolset()` method.
- Modify `src/marim_harness/runtime/controller.py` — `__init__` gains `lsp_toolset`; line 858 uses `compose_turn_toolsets`.
- Modify `src/marim_harness/runtime/harness.py` — pass `lsp_toolset=self.provider.lsp_toolset()` into `TurnController(...)`.
- Tests: `tests/test_lsp_toolset.py`, `tests/test_runtime_toolsets.py`, `tests/test_lsp_wiring.py`.

---

### Task 1: `build_lsp_toolset()`

**Files:**
- Modify: `src/marim_harness/tools/lsp_tools.py`
- Test: `tests/test_lsp_toolset.py`

**Interfaces:**
- Produces: `build_lsp_toolset() -> FunctionToolset[Deps]` — a `FunctionToolset` holding the six existing LSP functions (`goto_definition`, `find_references`, `hover`, `document_symbols`, `workspace_symbols`, `diagnostics`), all ungated (no `requires_approval`).

- [ ] **Step 1: Write the failing test**

`tests/test_lsp_toolset.py`:
```python
from marim_harness.tools.lsp_tools import build_lsp_toolset

_EXPECTED = {
    "goto_definition", "find_references", "hover",
    "document_symbols", "workspace_symbols", "diagnostics",
}


def test_build_lsp_toolset_has_the_six_tools():
    ts = build_lsp_toolset()
    assert set(ts.tools) == _EXPECTED


def test_lsp_tools_are_ungated():
    ts = build_lsp_toolset()
    for name in _EXPECTED:
        assert ts.tools[name].requires_approval is not True
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_toolset.py -v`
Expected: FAIL with `ImportError: cannot import name 'build_lsp_toolset'`.

- [ ] **Step 3: Write minimal implementation**

Add to `src/marim_harness/tools/lsp_tools.py` — extend the imports and append the builder at the end:
```python
from pydantic_ai.toolsets import FunctionToolset
```
```python
def build_lsp_toolset() -> FunctionToolset[Deps]:
    """The six LSP navigation tools as a single deferrable toolset for the main
    agent. Ungated (they are read-only). Registered here rather than via
    ``agent.tool`` so they can ride the per-turn tool-search deferral path
    (see ``runtime.toolsets.compose_turn_toolsets``). Each tool still guards
    ``ctx.deps.services.lsp is None``, so a toolset built while the manager is
    unavailable degrades gracefully. Sub-agents do NOT use this — they keep
    name-based registration (``provider.register_subagent``)."""
    ts: FunctionToolset[Deps] = FunctionToolset()
    ts.add_function(goto_definition)
    ts.add_function(find_references)
    ts.add_function(hover)
    ts.add_function(document_symbols)
    ts.add_function(workspace_symbols)
    ts.add_function(diagnostics)
    return ts
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_lsp_toolset.py -v`
Expected: PASS (2 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/tools/lsp_tools.py tests/test_lsp_toolset.py
git commit -m "feat(lsp): build_lsp_toolset() wrapping the six navigation tools"
```

---

### Task 2: `compose_turn_toolsets()`

**Files:**
- Create: `src/marim_harness/runtime/toolsets.py`
- Test: `tests/test_runtime_toolsets.py`

**Interfaces:**
- Consumes: `McpManager` (`live_toolsets()`, async `live_tool_count()`), `should_defer` (`mcp/manager.py`).
- Produces: `async def compose_turn_toolsets(mcp, lsp_toolset, lsp_count, policy, threshold) -> list[AbstractToolset[Deps]]` — the live MCP toolsets plus the LSP toolset (if any), deferred together as one `DeferredLoadingToolset(CombinedToolset(...))` when the *combined* tool count triggers `should_defer`, else returned inline. Returns `[]` when nothing is live. With `lsp_toolset=None`, reproduces `mcp.toolsets_for` exactly.

- [ ] **Step 1: Write the failing test**

`tests/test_runtime_toolsets.py`:
```python
import pytest
from pydantic_ai import CombinedToolset, DeferredLoadingToolset
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.runtime.toolsets import compose_turn_toolsets


class _FakeMcp:
    def __init__(self, toolsets, count):
        self._toolsets = list(toolsets)
        self._count = count

    def live_toolsets(self):
        return list(self._toolsets)

    async def live_tool_count(self):
        return self._count


def _lsp():
    ts = FunctionToolset()
    ts.add_function(lambda x: str(x), name="goto_definition")
    return ts


@pytest.mark.anyio
async def test_lsp_none_reproduces_live_when_under_threshold():
    a, b = FunctionToolset(), FunctionToolset()
    mcp = _FakeMcp([a, b], count=3)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 5)
    assert out == [a, b]


@pytest.mark.anyio
async def test_lsp_none_defers_live_when_over_threshold():
    a = FunctionToolset()
    mcp = _FakeMcp([a], count=10)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 5)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_lsp_inline_when_combined_under_threshold():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=3)  # 3 + 6 = 9 <= 10
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 10)
    assert out == [a, lsp]  # lsp present inline


@pytest.mark.anyio
async def test_lsp_count_tips_combined_over_threshold():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=5)  # mcp alone (5) <= 8, but 5 + 6 = 11 > 8
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 8)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)


@pytest.mark.anyio
async def test_lsp_only_empty_mcp_inline_under_threshold():
    lsp = _lsp()
    mcp = _FakeMcp([], count=0)
    out = await compose_turn_toolsets(mcp, lsp, 6, "auto", 10)
    assert out == [lsp]


@pytest.mark.anyio
async def test_all_empty_returns_empty():
    mcp = _FakeMcp([], count=0)
    out = await compose_turn_toolsets(mcp, None, 6, "auto", 10)
    assert out == []


@pytest.mark.anyio
async def test_policy_on_always_defers():
    a = FunctionToolset()
    lsp = _lsp()
    mcp = _FakeMcp([a], count=1)
    out = await compose_turn_toolsets(mcp, lsp, 6, "on", 999)
    assert len(out) == 1 and isinstance(out[0], DeferredLoadingToolset)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_runtime_toolsets.py -v`
Expected: FAIL with `ModuleNotFoundError: No module named 'marim_harness.runtime.toolsets'`.

- [ ] **Step 3: Write minimal implementation**

`src/marim_harness/runtime/toolsets.py`:
```python
"""Per-turn toolset composition for the main agent.

Folds the live MCP toolsets and the (optional) LSP toolset into the single list
passed to ``agent.run(toolsets=…)`` each turn, under ONE tool-search deferral
decision. LSP thus shares the MCP budget: below threshold it rides inline; above
it, MCP and LSP defer together behind one ToolSearch (riding an already-present
ToolSearch at ~zero marginal cost). Keeping this here — not on ``McpManager`` —
means the MCP manager never learns about LSP, and the controller stays thin.

With ``lsp_toolset=None`` this reproduces ``McpManager.toolsets_for`` exactly, so
disabling LSP tools is a no-op on the toolset path.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from pydantic_ai import CombinedToolset, DeferredLoadingToolset

from ..mcp.manager import should_defer

if TYPE_CHECKING:
    from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

    from ..mcp import McpManager
    from .deps import Deps


async def compose_turn_toolsets(
    mcp: McpManager,
    lsp_toolset: FunctionToolset[Deps] | None,
    lsp_count: int,
    policy: str,
    threshold: int,
) -> list[AbstractToolset[Deps]]:
    live = mcp.live_toolsets()
    extras = [lsp_toolset] if lsp_toolset is not None else []
    combined = [*live, *extras]
    if not combined:
        return []
    count = await mcp.live_tool_count() + (lsp_count if lsp_toolset is not None else 0)
    if should_defer(policy, count, threshold):
        return [DeferredLoadingToolset(CombinedToolset(combined))]
    return combined
```

- [ ] **Step 4: Run test to verify it passes**

Run: `uv run pytest --no-cov tests/test_runtime_toolsets.py -v`
Expected: PASS (7 tests).

- [ ] **Step 5: Commit**

```bash
git add src/marim_harness/runtime/toolsets.py tests/test_runtime_toolsets.py
git commit -m "feat(runtime): compose_turn_toolsets — unified MCP+LSP tool-search budget"
```

---

### Task 3: Wire LSP off static registration onto the per-turn path

**Files:**
- Modify: `src/marim_harness/tools/provider.py` (drop LSP block in `register()`; add `lsp_toolset()`)
- Modify: `src/marim_harness/runtime/controller.py` (`__init__` param + line 858)
- Modify: `src/marim_harness/runtime/harness.py` (inject `lsp_toolset` into `TurnController`)
- Test: `tests/test_lsp_wiring.py`

**Interfaces:**
- Consumes: `build_lsp_toolset` (Task 1), `compose_turn_toolsets` (Task 2), `LSP_TOOLS` (`tools/names.py`).
- Produces: `BuiltinToolProvider.lsp_toolset() -> FunctionToolset[Deps] | None`; `TurnController.__init__(..., lsp_toolset=None)`. After this task, the main agent no longer statically carries the six LSP tools — they arrive per turn via `compose_turn_toolsets`. Sub-agents unchanged.

This is one atomic task: dropping static registration and adding the per-turn path must land together, or LSP tools would vanish between commits.

- [ ] **Step 1: Write the failing test**

`tests/test_lsp_wiring.py`:
```python
import pytest
from pydantic_ai import Agent
from pydantic_ai.toolsets import FunctionToolset

from marim_harness.tools.names import LSP_TOOLS
from marim_harness.tools.provider import BuiltinToolProvider


def _registered_tool_names(register_lsp_tools: bool) -> set[str]:
    agent = Agent("test")
    BuiltinToolProvider(register_lsp_tools=register_lsp_tools).register(agent)
    # FunctionToolset backing the agent's directly-registered tools.
    return set(agent._function_toolset.tools)  # noqa: SLF001


def test_main_agent_no_longer_statically_registers_lsp():
    names = _registered_tool_names(register_lsp_tools=True)
    assert not (LSP_TOOLS & names), "LSP tools should move off static registration"
    # Non-LSP builtins are still present.
    assert "read_file" in names and "bash" in names


def test_lsp_toolset_present_when_enabled():
    ts = BuiltinToolProvider(register_lsp_tools=True).lsp_toolset()
    assert isinstance(ts, FunctionToolset)
    assert LSP_TOOLS <= set(ts.tools)


def test_lsp_toolset_none_when_disabled():
    assert BuiltinToolProvider(register_lsp_tools=False).lsp_toolset() is None
```

> Note (confirmed on 1.107.0): `agent._function_toolset` is an `_AgentFunctionToolset` and `.tools` is the dict of directly-registered tool names — verified to hold `read_file`/`bash` and (before this change) the LSP names. The assertion intent: the six LSP names are NOT among the agent's directly-registered tools, but `read_file`/`bash` are.

- [ ] **Step 2: Run test to verify it fails**

Run: `uv run pytest --no-cov tests/test_lsp_wiring.py -v`
Expected: FAIL — `test_main_agent_no_longer_statically_registers_lsp` fails (LSP still registered) and `lsp_toolset` attribute doesn't exist yet.

- [ ] **Step 3: Implement — provider.py**

In `src/marim_harness/tools/provider.py`, **remove** this block from `register()`:
```python
        if self._register_lsp_tools:
            agent.tool(lsp_tools.goto_definition)
            agent.tool(lsp_tools.find_references)
            agent.tool(lsp_tools.hover)
            agent.tool(lsp_tools.document_symbols)
            agent.tool(lsp_tools.workspace_symbols)
            agent.tool(lsp_tools.diagnostics)
```
Add a method to `BuiltinToolProvider` (near `register`):
```python
    def lsp_toolset(self) -> "FunctionToolset[Deps] | None":
        """The LSP navigation tools as a deferrable toolset for the *main* agent,
        or None when LSP tools are disabled. Built from the same
        ``_register_lsp_tools`` flag that used to gate their static registration,
        so the two never drift. The Harness injects the result into TurnController,
        which routes it through ``compose_turn_toolsets`` per turn. Sub-agents are
        unaffected — ``register_subagent`` still name-registers LSP."""
        return lsp_tools.build_lsp_toolset() if self._register_lsp_tools else None
```
Add the imports needed for the annotation (top of `provider.py`):
```python
from ..runtime.deps import Deps  # if not already imported
from pydantic_ai.toolsets import FunctionToolset
```
(If importing `Deps`/`FunctionToolset` at module top risks a cycle or is unused elsewhere, keep the return annotation as a string as shown and import under `TYPE_CHECKING`.)

- [ ] **Step 4: Implement — controller.py**

Add a runtime import near the top of `src/marim_harness/runtime/controller.py` (NOT under `TYPE_CHECKING` — used at call time):
```python
from ..tools.names import LSP_TOOLS
from .toolsets import compose_turn_toolsets
```
Under the existing `TYPE_CHECKING` block, add:
```python
    from pydantic_ai.toolsets import FunctionToolset
```
In `TurnController.__init__`, add the parameter (keyword, defaulted so existing constructions/tests don't break) and store it:
```python
        lsp_toolset: "FunctionToolset[Deps] | None" = None,
```
```python
        self.lsp_toolset = lsp_toolset
```
Replace the line-858 call:
```python
            toolsets = await self.mcp.toolsets_for(
                self.deps.workspace.tool_search,
                self.deps.workspace.tool_search_threshold,
            )
```
with:
```python
            toolsets = await compose_turn_toolsets(
                self.mcp,
                self.lsp_toolset,
                len(LSP_TOOLS),
                self.deps.workspace.tool_search,
                self.deps.workspace.tool_search_threshold,
            )
```

- [ ] **Step 5: Implement — harness.py**

In `src/marim_harness/runtime/harness.py`, in the `TurnController(...)` construction (around line 362), add the injected toolset (built from the provider already stored as `self.provider`):
```python
        self.turn_controller = TurnController(
            agent=self.agent,
            session=self.session,
            checkpoints=self.checkpoints,
            hooks=self.hooks,
            mcp=self.mcp,
            deps=self.deps,
            lsp_toolset=self.provider.lsp_toolset(),
            get_model=lambda: self.current_model,
        )
```

- [ ] **Step 6: Run the wiring test + full verification**

Run: `uv run pytest --no-cov tests/test_lsp_wiring.py -v` → PASS (3 tests).
Then the full CI order (regression is the point — existing LSP behavior tests and all sub-agent tests must be unchanged and green):
```bash
uv run ruff check src tests
uv run pyright
uv run pytest
```
Expected: all clean/green, no regressions.

- [ ] **Step 7: Commit**

```bash
git add src/marim_harness/tools/provider.py src/marim_harness/runtime/controller.py \
        src/marim_harness/runtime/harness.py tests/test_lsp_wiring.py
git commit -m "feat(lsp): route main-agent LSP tools through the per-turn deferral path"
```

---

## Manual verification (after Task 3)

Confirm LSP tools still reach the model and defer correctly, by constructing the composed toolsets directly:
```bash
uv run python -c "
import asyncio
from pydantic_ai import DeferredLoadingToolset
from marim_harness.tools.provider import BuiltinToolProvider
from marim_harness.runtime.toolsets import compose_turn_toolsets

class FakeMcp:
    def live_toolsets(self): return []
    async def live_tool_count(self): return 0

async def main():
    lsp = BuiltinToolProvider(register_lsp_tools=True).lsp_toolset()
    # small surface, auto policy -> LSP inline
    inline = await compose_turn_toolsets(FakeMcp(), lsp, 6, 'auto', 100)
    print('inline:', [type(t).__name__ for t in inline], 'lsp tools:', sorted(inline[0].tools))
    # policy on -> deferred
    deferred = await compose_turn_toolsets(FakeMcp(), lsp, 6, 'on', 100)
    print('deferred:', isinstance(deferred[0], DeferredLoadingToolset))

asyncio.run(main())
"
```
Expected: `inline` is a single `FunctionToolset` exposing the six LSP tool names; `deferred` is True. (The provider path is exercised end-to-end; the controller wiring is a one-line call of the same function.)

## Self-Review

**Spec coverage:**
- `build_lsp_toolset()` (six tools, ungated) → Task 1. ✓
- `compose_turn_toolsets` with unified `should_defer` budget + the full deferral table → Task 2 (all rows covered incl. the LSP-count-tips-over case and `lsp_toolset=None` reproduces today). ✓
- Drop LSP from `provider.register()`, keep `register_subagent` → Task 3. ✓
- `provider.lsp_toolset()` single-source flag; controller `__init__` + line-858 swap; harness injection → Task 3. ✓
- Sub-agents untouched → no sub-agent file modified in any task; regression asserted by the full suite in Task 3. ✓
- `CombinedToolset`+`FunctionToolset` composition (Open Item 1) → resolved by spike, stated in Verified Facts; no fallback code needed. ✓
- Single-source `register_lsp = lsp_enabled and lsp_tools_enabled` (Open Item 2) → resolved: the provider's existing `_register_lsp_tools` is the one source; `lsp_toolset()` reads it. ✓

**Placeholder scan:** No TBD/TODO. The one runtime-introspection uncertainty (`agent._function_toolset` attribute name in the wiring test) is called out explicitly with the assertion intent and a discovery instruction, not left as a guess.

**Type consistency:** `compose_turn_toolsets(mcp, lsp_toolset, lsp_count, policy, threshold)` signature identical across Task 2 (definition) and Task 3 (controller call, passing `len(LSP_TOOLS)`); `lsp_toolset` typed `FunctionToolset[Deps] | None` in Task 2, Task 3 provider method, and Task 3 controller param; `build_lsp_toolset` / `lsp_toolset()` / `compose_turn_toolsets` names consistent across tasks.
