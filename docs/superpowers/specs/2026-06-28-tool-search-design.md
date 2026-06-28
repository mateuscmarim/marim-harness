# Tool search for MCP/plugin tools

**Status:** approved (design)
**Date:** 2026-06-28

## Problem

Every `agent.run()` exposes the full tool surface to the model. The ~30 builtin
tools are small, stable, and live in the cached system-prompt prefix — cheap. The
problem is **MCP servers + plugins**: their tool count is unbounded, their JSON
schemas are large, and loading all of them on every request burns tokens and
dilutes the model's attention. (This session's own deferred-tool list — gmail,
calendar, drive, excalidraw, mddocs, agentmemory, chrome… — is the poster child.)

We want to defer the variable MCP/plugin surface behind a search-and-load step,
while keeping the builtin toolset always loaded.

## Key finding: Pydantic AI ships native tool search

We do **not** build a search engine. Pydantic AI already provides:

- `DeferredLoadingToolset(wrapped, tool_names=...)` — wraps a toolset and marks its
  tools `defer_loading=True`, hiding them from the model.
- `ToolSearchToolset` + `ToolSearchTool` — the search tool that reveals deferred
  tools, with `ToolSearchLocalStrategy` (search over name/description) and
  provider-native strategies.
- `ToolSearchCallPart` / `ToolSearchReturnPart` — first-class message-history parts,
  so search is wired into the conversation protocol.

`MCPToolset.list_tools()` returns name + description (cached, no full schema), which
is what the local search strategy indexes.

So this feature is **policy + composition + config + UI**, not a search engine.

## Scope decisions (settled in brainstorming)

- **Deferral scope:** MCP + plugin tools only. Builtins are never deferred — they
  stay registered on the Agent and always loaded (cache-stable, always relevant).
- **Activation:** threshold-based `auto` by default, with an `off`/`auto`/`on`
  override and a configurable threshold.
- **Sub-agents:** unchanged. They already receive curated, minimal tool grants, so
  no deferral; tool search applies to the main agent only.

## Design

### 1. Policy (config)

Two new `ModelConfig` knobs, parsed in `_common_kwargs()` like `default_mode`:

- `MARIM_TOOL_SEARCH`: `off` | `auto` | `on` (default `auto`). Invalid → `auto`
  (warned).
- `MARIM_TOOL_SEARCH_THRESHOLD`: positive int, default `15`. Consulted only in
  `auto`. Added to `config/env.py`'s `_POSITIVE_INT_KEYS` so a non-positive/garbage
  value is dropped and the default applies.

Neither key is security-sensitive (deferring a tool does not remove it — it stays
reachable via search), so **neither goes in `_PROJECT_ENV_BLOCKLIST`**; a project
`.env` may set them freely.

### 2. Decision point — the turn loop

In `runtime/controller.py`, where `toolsets = self.mcp.live_toolsets()` is built
(≈line 613), apply policy once per run:

- `off` → pass MCP toolsets unchanged (today's behavior).
- `on` → wrap the live MCP/plugin toolsets in `DeferredLoadingToolset` and append a
  `ToolSearchToolset` (local strategy) to the toolset list.
- `auto` → compute the live MCP tool count; if `count > threshold`, behave as `on`,
  else as `off`.

Builtins are never wrapped. The Agent's registered toolset is untouched; all of this
happens in the per-run `toolsets=` argument, which marim already varies per run.

### 3. Counting (MCP manager)

Add two helpers to `mcp/manager.py`:

- `async live_tool_count() -> int` — sum `list_tools()` across non-disabled live
  servers (cheap: names only, cached).
- `deferred_toolsets(strategy) -> list` — return the live MCP toolsets wrapped in
  `DeferredLoadingToolset` plus a `ToolSearchToolset`, for the `on`/`auto`-deferred
  path. Keeps the Pydantic AI composition in the MCP layer rather than the
  controller.

The controller asks the manager for either `live_toolsets()` (today) or
`deferred_toolsets(...)` based on the policy decision, which it computes with
`live_tool_count()` when in `auto`.

### 4. Config surface + settings screen

- `config show` (text + JSON) reports `tool_search` and `tool_search_threshold`.
- `config set` accepts both keys: `MARIM_TOOL_SEARCH` validated against
  `off/auto/on` (the existing `_ENUM_KEYS` mechanism); `MARIM_TOOL_SEARCH_THRESHOLD`
  validated as a positive int.
- The **Config section** of the settings screen gains a "Tool search" selector
  (off/auto/on RadioSet) and a "Tool-search threshold" integer Input, both written
  to the global `.env` on Save (next-launch), like the other env settings.

### 5. Resumability (the integration risk)

`ToolSearchCallPart`/`ToolSearchReturnPart` are new message-history parts. marim
persists history through Pydantic AI's generic message dump, so they should
round-trip — but this is the one place native tool search touches marim's
load-bearing invariants (history serialization + "history never ends on an
unanswered tool call"). It gets an explicit test: run a turn that issues a tool
search, persist, reload, and continue — assert the history round-trips and the turn
resumes cleanly.

## Files touched

- `config/model.py` — two knobs. The `off/auto/on` enum needs its own validated
  parse: generalize `_mode_env` into an `_enum_env(name, default, valid)` helper
  (so `default_mode` keeps using it with `{ask,auto,plan}` and tool-search uses it
  with `{off,auto,on}`), rather than reusing the mode-specific validator as-is. The
  threshold reuses `_int_env`.
- `config/env.py` — add `MARIM_TOOL_SEARCH_THRESHOLD` to `_POSITIVE_INT_KEYS`
  (note: **not** the blocklist).
- `interfaces/cli/config.py` — show + set for both keys.
- `interfaces/tui/settings.py` — Config-section selector + threshold input + save.
- `mcp/manager.py` — `live_tool_count()` and `deferred_toolsets()`.
- `runtime/controller.py` — the per-run policy decision.
- Tests (below).

## Testing

- **Policy parsing:** `MARIM_TOOL_SEARCH` off/auto/on + invalid→auto;
  `MARIM_TOOL_SEARCH_THRESHOLD` valid/garbage→default.
- **Threshold boundary:** count ≤ threshold loads MCP normally; count > threshold
  defers (the decision helper is pure/unit-testable given a count).
- **Composition:** the deferred path yields a `ToolSearchToolset` + wrapped MCP
  toolsets; the `off` path yields the plain toolsets.
- **Resumability:** persist + reload across a tool-search round-trip (see §5).
- **Config CLI + settings screen:** set/show both keys; settings-screen save
  round-trip.
- CI order: `ruff` → `pyright` → `pytest`.

## Out of scope (YAGNI)

- Deferring builtins (always loaded).
- Token-size-based thresholds (count is simpler; revisit if needed).
- Sub-agent tool search (their reach is already curated).
- Provider-native (server-side) search strategies — start with the local strategy;
  native strategies can be a follow-up if a provider supports them.
