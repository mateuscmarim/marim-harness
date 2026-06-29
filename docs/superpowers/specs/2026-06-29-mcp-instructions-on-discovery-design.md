# MCP server instructions on discovery

**Status:** approved (design)
**Date:** 2026-06-29
**Builds on:** tool search (`2026-06-28-tool-search-design.md`) and the discovery catalog (`2026-06-28-tool-catalog-design.md`).

## Problem

MCP servers can send an `instructions` block at initialize — server-authored "how to
use my tools" guidance (workflow order, arg conventions, gotchas like "patch, don't
rewrite"). marim does **not** surface these today (`include_instructions` defaults to
`False` and marim never sets it). So when the model uses an MCP server's tools it gets
no usage guidance, which mostly bites *multi-step* workflows and weaker models.

The obvious fixes are both unattractive:
- **Always include all servers' instructions** (`include_instructions=True`) — re-bloats
  the prompt for every configured server, including deferred ones the model never uses,
  undoing tool search's savings.
- **Per-server opt-in flag** — makes the user decide, per MCP server, whether to include
  instructions. The user explicitly does not want that per-MCP busywork.

## Goal

Zero-config and token-efficient: when the model **discovers** a deferred MCP server's
tools via tool search, automatically surface *that server's* instructions — and only
then. No per-server config; nothing for undiscovered servers.

## Feasibility (verified against pydantic_ai 1.107)

- `RunContext.discovered_tool_names: set[str]` — pre-computed from message history, readable
  inside a dynamic `@agent.instructions` closure. Tells us exactly which deferred tools
  have been revealed this run.
- `MCPServer.instructions` (base-class property) returns the server's init-time instructions
  (`None` if the server sent none; raises `AttributeError` before init).
- marim builds every MCP server with `tool_prefix=<server name>`, and pydantic_ai names a
  tool `f"{tool_prefix}_{name}"`. So a discovered tool `mddocs_doc_index` maps to the server
  whose `tool_prefix` is `mddocs`.
- Dynamic instruction output is marked `dynamic=True`; Anthropic/OpenAI prompt caching keeps
  the static prefix cached and treats dynamic instructions as append-only/uncached, so adding
  this does **not** bust the cached prefix. (It is re-sent each turn — see the cost note.)

## Design

### 1. Renderer (pure)

`render_discovered_instructions(servers: list[tuple[str, str]]) -> str` in `mcp/catalog.py`
(next to `render_tool_catalog`). Input is `(server_name, instructions_text)` pairs already
filtered to discovered servers with non-empty instructions.

```
Usage guidance for the MCP servers you've loaded (follow it for those tools):

## mddocs
<mddocs instructions, truncated to _INSTRUCTIONS_CAP chars + "\n…(truncated)" if longer>

## agentmemory
<…>
```

- Servers sorted by name for deterministic output.
- Each server's instructions truncated to `_INSTRUCTIONS_CAP = 2000` chars, with a
  `\n…(truncated)` marker when clipped. Bounds the recurring cost (see note); a per-server
  cap mirrors the catalog's per-server name cap.
- Empty input → `""`.

### 2. Manager helper

`discovered_server_instructions(discovered: set[str]) -> list[tuple[str, str]]` on
`McpManager`. For each non-disabled live server with a `tool_prefix`, if any discovered tool
name starts with `f"{prefix}_"`, best-effort read `server.instructions`; include
`(server_name, text)` only when `text` is a non-empty string. Best-effort: a server whose
`.instructions` raises (not yet initialized) or returns `None`/"" contributes nothing — a
broken/quiet server never breaks a turn. Servers sorted for stable order.

### 3. The instruction closure

A new async `@agent.instructions` closure `_discovered_instructions(ctx)` in
`register_instructions` (which already holds the MCP manager):

```python
@agent.instructions
async def _discovered_instructions(ctx: RunContext[Deps]) -> str:
    discovered = getattr(ctx, "discovered_tool_names", None) or set()
    if not discovered:
        return ""
    return render_discovered_instructions(
        mcp_manager.discovered_server_instructions(discovered)
    )
```

Naturally gated: nothing discovered (tool search off, or the model hasn't searched yet) →
empty string, nothing injected. No interaction with the `tool_search` policy needed beyond
that — discovery only happens when tools are deferred.

### 4. Cost note (documented, accepted for v1)

Because dynamic instructions aren't cached, a discovered server's (capped) instructions are
re-sent on every subsequent request that session. This is intentional: the guidance must
*persist* while the model works through a multi-step workflow. The `_INSTRUCTIONS_CAP` bounds
the per-turn cost. A more cache-efficient variant — injecting the instructions into the
message history at the discovery moment (append-only, cacheable) — is a known future
optimization, deliberately out of scope for v1 (it needs custom wiring at the search-return
point).

## Files touched

- `mcp/catalog.py` — `render_discovered_instructions` + `_INSTRUCTIONS_CAP`.
- `mcp/manager.py` — `discovered_server_instructions`.
- `runtime/instructions.py` — the `_discovered_instructions` async closure.
- Tests (below).

## Testing

- **Unit:**
  - `render_discovered_instructions`: sorting, truncation at the cap with the marker,
    no-marker when under cap, empty input → "".
  - `discovered_server_instructions`: prefix matching (a discovered `mddocs_x` selects the
    `mddocs` server); best-effort (a server whose `.instructions` raises, or returns
    `None`/"", is skipped); only discovered servers included.
  - `_discovered_instructions` gating: empty `discovered_tool_names` → ""; populated → the
    rendered block.
- **Live verification (success criterion):** a capable model (sonnet) on a deliberately
  *multi-step* mddocs task where mddocs's instructions matter (e.g. an operation its
  guidance governs). Confirm from the session that (a) after the search-discovery turn the
  server's instructions appear in the request, and (b) the model's subsequent actions follow
  them. Also confirm a no-discovery run injects nothing.
- CI order: `ruff` → `pyright` → `pytest`.

## Out of scope (YAGNI)

- Per-server opt-in/opt-out (the whole point is zero-config).
- Always-on global `include_instructions` (re-bloats; fights tool search).
- The cacheable append-at-discovery optimization (future; needs search-return wiring).
- Making the 2000-char cap an env knob.
