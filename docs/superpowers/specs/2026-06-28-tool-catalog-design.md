# Tool-search discovery catalog

**Status:** approved (design)
**Date:** 2026-06-28
**Builds on:** the tool-search feature (`docs/superpowers/specs/2026-06-28-tool-search-design.md`).

## Problem

Tool search defers the MCP/plugin surface, but Pydantic AI's default **fully
hides** the deferred tools: the model sees only a generic `search_tools` function
("there are additional tools not yet visible to you"). It has no idea *what* is
hidden — no names, no categories. So to use a deferred tool the model must make two
unaided leaps: (1) "my loaded tools can't do this," and (2) "...but a *hidden* tool
might, so I should search" — and then guess the right query words. Weaker models
often skip the second leap and improvise (e.g. with `bash`) instead of searching.
This is **blind search**, and it's why the agent struggles to know *when* to search.

(Note: the tool-search e2e runs that looked clean used a primed prompt — "you may
need to discover the right tool first" — which masked this. Unprompted, the model
under-searches.)

## Goal

Give the model cheap **awareness** of what's discoverable so "should I search?" stops
being a guess: inject a server-grouped catalog of the deferred MCP tool **names**
(schemas still withheld) plus a short proactive-search instruction — but only when
tool search is actually deferring, and placed in the cached prompt prefix so it's
paid for once.

## Design

### 1. The catalog renderer (pure)

`render_tool_catalog(groups: dict[str, list[str]]) -> str` — given an ordered map of
`server_name -> [tool_names]`, render a deterministic block:

```
Additional MCP tools are available but not loaded by default. Use the search_tools
function to discover and load them (query with words from the names below) before
concluding a capability is unavailable. Available tools by server:
- agentmemory: memory_recall, memory_save, memory_smart_search, …
- mddocs: mddocs_doc_index, mddocs_edit_doc, mddocs_grep_docs, … (+5 more)
- nasa-server: …
```

- The opening sentence **is** the proactive-search nudge (one injection, not two).
- **Per-server cap:** at most `_CATALOG_PER_SERVER_CAP = 12` names per server; if a
  server has more, append ` (+N more)`. Per-server (not a global cap) so every server
  stays represented. Hardcoded sensible default, not an env knob (YAGNI).
- **Deterministic:** servers and tool names sorted, so the rendered text is
  byte-identical across turns when the server set is unchanged → the cached prefix
  stays stable.
- Returns `""` for empty `groups`.

### 2. MCP manager helper

`async live_tools_by_server() -> dict[str, list[str]]` on `McpManager` — for each
non-disabled live server, its sorted tool names from the cached `list_tools()`.
Best-effort, mirroring `live_tool_count`'s error handling: a server that can't list
contributes no entry rather than failing. `live_tool_count` is refactored to derive
its total from this helper (DRY: count == sum of the lengths).

### 3. The dynamic instruction

A new async `@agent.instructions` closure `_tool_catalog` in
`runtime/instructions.py` `register_instructions` (which already receives the
`McpManager`). It is **separate** from the existing `_mcp_index` closure (that one is
about granting servers to sub-agents — a different concern; leave it untouched).

```
@agent.instructions
async def _tool_catalog(ctx: RunContext[Deps]) -> str:
    ws = ctx.deps.workspace
    groups = await mcp_manager.live_tools_by_server()
    total = sum(len(v) for v in groups.values())
    if not should_defer(ws.tool_search, total, ws.tool_search_threshold):
        return ""
    return render_tool_catalog(groups)
```

So the catalog appears **only when tool search is deferring this run** — the exact
same `should_defer(policy, count, threshold)` gate the controller uses for
`toolsets_for`, so the two always agree (catalog shown ⇔ tools deferred). Off /
below-threshold / no-MCP sessions inject nothing and pay nothing.

Pydantic AI awaits async instruction functions, so the `await` is fine. The closure
runs per model request, but `list_tools()` is cached and the output is deterministic,
so the cached prefix holds; a `/mcp` toggle changes the server set and the catalog
refreshes on the next request (one cache miss, then stable again).

### 4. Consistency & caching notes

- The catalog gate and the deferral decision share `should_defer` + the live count;
  minor double-compute, both cheap (cached `list_tools`), no drift.
- Deterministic rendering is what makes this safe for prompt caching — the whole
  reason for placing it in the prefix rather than the per-turn envelope.

## Files touched

- `mcp/catalog.py` *(new)* — the pure `render_tool_catalog(groups)` renderer and the
  `_CATALOG_PER_SERVER_CAP = 12` constant. Isolated and unit-tested directly.
- `mcp/manager.py` — `live_tools_by_server()`; refactor `live_tool_count` to reuse it.
  (`should_defer` already lives here from the tool-search feature.)
- `runtime/instructions.py` — the `_tool_catalog` async instruction in
  `register_instructions`, importing `render_tool_catalog` from `..mcp.catalog` and
  `should_defer` from `..mcp.manager`.
- Tests (below).

## Testing

- **Unit:**
  - `render_tool_catalog`: grouping, sorting/determinism, the per-server cap and
    `(+N more)` suffix, empty-input → `""`.
  - `live_tools_by_server`: fake servers, best-effort on a server that raises.
  - `_tool_catalog` gating: returns the catalog when `should_defer` is true (policy
    on, or auto+over-threshold), and `""` when off / below-threshold / no servers.
- **Live verification (the actual success criterion):** re-run the headless e2e on
  `MARIM_MODEL=openrouter/owl-alpha` with `MARIM_TOOL_SEARCH=on` and an **unprompted**
  task — one that needs an MCP tool but does NOT say "discover the tool first" (e.g.
  "How many document projects do I have?"). Confirm from the session history that the
  model calls `search_tools` on its own and then the real MCP tool. If unprompted
  behavior doesn't change, the feature failed regardless of green unit tests.
- CI order: `ruff` → `pyright` → `pytest`.

## Out of scope (YAGNI)

- Making the per-server cap an env knob.
- Per-tool descriptions in the catalog (names only — the chosen granularity).
- Server-level capability summaries (we list names, not hand-written summaries).
- A semantic/embedding search strategy (improves match quality, not "when to search").
- Refreshing/rebuilding on `/mcp` toggle beyond the natural per-request recompute.
