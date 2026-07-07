# LSP tools → deferrable toolset (unified with MCP's tool-search budget)

**Date:** 2026-07-07
**Status:** Design approved, ready for implementation plan
**Scope:** Move the **main agent's** six LSP navigation tools from static `agent.tool()` registration to a Pydantic AI `FunctionToolset` that is routed through the per-turn tool-search deferral path, sharing one budget with the MCP surface. Goal: consolidate the scattered LSP gating **and** get the six tools out of the base prompt when the combined tool surface is large. Sub-agents are out of scope (unchanged).

## Goal

The six LSP navigation tools (`goto_definition`, `find_references`, `hover`, `document_symbols`, `workspace_symbols`, `diagnostics`) are always in the main-loop base prompt today — six tool schemas paid on every turn. This spec makes them a cohesive, deferrable unit: below a tool-count threshold they stay inline (as today, functionally); above it they defer behind Pydantic AI's ToolSearch **together with** the MCP surface, riding an already-present ToolSearch at near-zero marginal cost.

### Why this is worth doing

- **Token savings on the main loop.** When a project has a large MCP surface that already defers, LSP joins that ToolSearch for free — no extra ToolSearch tool, no separate search round-trip.
- **Cleanup.** LSP gating is currently scattered: an `if self._register_lsp_tools:` block in `provider.register`, the `LSP_TOOLS` frozenset special-cased in `register_subagent`, and the flag threaded from bootstrap. The main-agent path collapses into one `build_lsp_toolset()` plus one composition function.

### Non-goals (YAGNI)

- **Sub-agents.** They keep static name-registration of LSP (grant still rides each spec's tool allow-list via `register_subagent`/`_SUBAGENT_FNS`). The same `lsp_tools.py` functions feed both paths, so implementations stay single-source. Redesigning per-spawn LSP grant to a toolset is explicitly out of scope — the deferral payoff is a main-loop concern and sub-agents are short-lived.
- **Generalizing** `compose_turn_toolsets` to an arbitrary list of deferrable builtin toolsets. It takes one optional `lsp_toolset`; generalizing later (for forge/job tools) is a one-line signature change. Naming it generically now is speculative.
- Deferring forge or any other builtin group here.
- Changing `tool_search` policy / threshold config or defaults — LSP simply joins the existing budget.

## Context (current mechanics)

- **Main agent:** builtins are registered statically via `provider.register(agent)` (`agent.tool(fn)` per tool, LSP behind `if self._register_lsp_tools:`). MCP tools flow **per turn**: `controller.py:858` calls `toolsets = await self.mcp.toolsets_for(policy, threshold)` and passes them to `agent.run(toolsets=…)`. Builtins on the Agent are unaffected by that path.
- **Deferral machinery:** `McpManager.toolsets_for(policy, threshold)` returns `[DeferredLoadingToolset(CombinedToolset(live))]` when `should_defer(policy, live_tool_count, threshold)` fires, else the plain `live_toolsets()`. `should_defer` is public in `mcp/manager.py`.
- **LSP tools** (`tools/lsp_tools.py`) are **stateless closures**: each reaches the manager via `ctx.deps.services.lsp` and guards `if … is None: return _LSP_UNAVAILABLE`. No state to bind into a toolset — a toolset stays Deps-based.
- **Sub-agents:** static tools via `provider.register_subagent(sub, effective_tools(defn, …))` (name-based, LSP filtered by allow-list + `register_lsp_tools`); per-turn MCP via `mcp.granted_toolsets(...)` → `sub.run(toolsets=granted)`.
- `TurnController` (built at `harness.py:362`) holds `self.mcp` and `self.deps`.

## Architecture

Surgical; no new subsystem. The LSP tool *functions* remain in `tools/lsp_tools.py` unchanged; only how the **main agent** receives them changes.

Files:
- `tools/lsp_tools.py` — add `build_lsp_toolset() -> FunctionToolset[Deps]` wrapping the six existing functions (ungated — they are reads).
- `tools/provider.py` — `register()` drops the LSP `agent.tool(...)` block (main agent no longer statically registers LSP). `register_subagent()` unchanged. `self._register_lsp_tools` retained (still gates the sub-agent path and whether the toolset is built).
- `runtime/toolsets.py` *(new leaf module)* — `compose_turn_toolsets(mcp, lsp_toolset, lsp_count, policy, threshold)`: folds the live MCP toolsets and the LSP toolset into the per-turn list under one unified `should_defer` decision. Keeps `McpManager` MCP-only (it never learns about LSP) and the controller thin.
- `runtime/controller.py` — `__init__` gains `lsp_toolset: FunctionToolset[Deps] | None = None`; line 858 calls `compose_turn_toolsets` instead of `mcp.toolsets_for`.
- `runtime/harness.py` — `build_collaborators` builds `lsp_toolset = build_lsp_toolset()` when LSP tools are enabled and injects it into `TurnController(...)`.

## The composition function & unified deferral

```python
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

`lsp_count` is `len(LSP_TOOLS)` (6) from `names.py`. Deferral behavior:

| MCP surface | LSP | combined count vs threshold | Result |
|---|---|---|---|
| any | `None` (off) | — | **exactly today's `toolsets_for`** (LSP absent — expected when off) |
| empty | on | 6 < threshold | `[lsp_toolset]` inline (6 tools in prompt, as today) |
| present | on | under | `[*mcp_live, lsp_toolset]` all inline |
| present | on | **over** | `[DeferredLoadingToolset(CombinedToolset([*mcp_live, lsp_toolset]))]` — MCP + LSP defer together behind one ToolSearch |
| empty | on | 6 ≥ threshold (low threshold) | LSP alone deferred — rare, accepted under the unified policy |

Guarantees:
- **No regression when LSP is off** — `lsp_toolset=None` reproduces `mcp.toolsets_for` behavior in both branches.
- **The payoff** — when a real MCP surface already defers, LSP joins that existing ToolSearch at ~zero marginal cost; when the surface is small, LSP stays inline (no pointless ToolSearch for six tools).

**Deliberate behavioral shift:** below threshold, LSP tools now arrive as a per-turn `toolsets=` entry rather than static `agent.tool()` registrations — functionally identical availability (the same mechanism MCP/forge use), but no longer in the cacheable static tool list. The only prompt-cache churn is at the threshold boundary, which already exists for MCP.

## Wiring changes

**`provider.register()`** — delete the LSP block:
```python
# REMOVED (main agent no longer statically registers LSP):
if self._register_lsp_tools:
    agent.tool(lsp_tools.goto_definition)
    ...  # 6 tools
```
`register_subagent()` unchanged. `self._register_lsp_tools` retained.

**`build_lsp_toolset()`** in `tools/lsp_tools.py`:
```python
def build_lsp_toolset() -> FunctionToolset[Deps]:
    ts: FunctionToolset[Deps] = FunctionToolset()
    ts.add_function(goto_definition)
    ts.add_function(find_references)
    ts.add_function(hover)
    ts.add_function(document_symbols)
    ts.add_function(workspace_symbols)
    ts.add_function(diagnostics)
    return ts
```
Ungated. Each function already guards `ctx.deps.services.lsp is None`, so a stale toolset degrades gracefully.

**`build_collaborators` (harness.py)** — build under the *same* condition the provider uses (sourced from one place, not a duplicated boolean):
```python
register_lsp = cfg.lsp_enabled and cfg.lsp_tools_enabled
lsp_toolset = build_lsp_toolset() if register_lsp else None
...
self.turn_controller = TurnController(..., mcp=mcp, lsp_toolset=lsp_toolset, ...)
```

**`TurnController.__init__`** — add `lsp_toolset: FunctionToolset[Deps] | None = None` → `self.lsp_toolset = lsp_toolset`. Line 858 becomes:
```python
toolsets = await compose_turn_toolsets(
    self.mcp, self.lsp_toolset, len(LSP_TOOLS),
    self.deps.workspace.tool_search, self.deps.workspace.tool_search_threshold,
)
```

## Edge cases

- **Stale/None LSP manager** — unchanged: each function guards `ctx.deps.services.lsp is None` and returns `_LSP_UNAVAILABLE`.
- **`CombinedToolset` mixing a `FunctionToolset` (LSP) with MCP server toolsets** — the one assumption to verify with a quick spike before implementing (both are `AbstractToolset`, so it should compose). Fallback if it doesn't: append a separate `DeferredLoadingToolset(lsp_toolset)` to the MCP list — still one unified budget, just two deferred entries.
- **Sub-agents** — untouched; still name-register LSP and graft MCP via `granted_toolsets`. No sub-agent test changes.
- **`diagnostics` tool vs diagnostics-on-edit** — separate concern; diagnostics-on-edit rides `deps.services.lsp` on write/edit gated by `lsp_enabled`, not by tool registration. Unaffected.

## Testing

- **`tests/test_runtime_toolsets.py`** (new) — `compose_turn_toolsets` against a fake mcp (exposing `live_toolsets()` and async `live_tool_count()`):
  - `lsp_toolset=None` + MCP present → identical to `mcp.toolsets_for` (both deferred and live branches).
  - LSP present, combined **under** threshold → `[*mcp_live, lsp_toolset]` (LSP present inline).
  - LSP present, combined **over** threshold → single `DeferredLoadingToolset` whose combined set includes the LSP toolset.
  - all empty → `[]`.
  - Assert the count fed to `should_defer` is `mcp_count + 6` (unified budget): MCP just-under + 6 tips it over ⇒ deferral fires.
- **`build_lsp_toolset()`** test (in `tests/test_lsp_tools.py` or a new file) — returns a `FunctionToolset` with exactly the six expected tool names, none `requires_approval`.
- **Wiring test** — with LSP tools enabled, the main agent does **not** statically carry the six LSP tools (moved to the per-turn toolset) and `TurnController` received a non-None `lsp_toolset`; with LSP off, `lsp_toolset` is None and behavior matches today. (Assert via the provider's registered tool names + the injected toolset, mirroring the forge wiring test's `agent.toolsets` check.)
- **Spike (not committed):** verify `DeferredLoadingToolset(CombinedToolset([function_toolset, mcp_toolset]))` builds, before implementing (validates the Section-4 assumption).
- **Regression:** full suite green — existing LSP tool-behavior tests and all sub-agent tests pass unchanged.

CI order `ruff → pyright → pytest` on Python 3.10/3.12/3.14 (all 3.10-safe).

## Open items for the implementation plan

- Resolve the spike result: confirm `CombinedToolset([function_toolset, *mcp_toolsets])` composes; if not, use the two-deferred-entries fallback and adjust `compose_turn_toolsets`'s deferred branch accordingly.
- Confirm the exact single source for the `register_lsp = lsp_enabled and lsp_tools_enabled` flag so the provider's `register_lsp_tools` and the toolset build stay in lockstep (avoid deriving the boolean twice).
