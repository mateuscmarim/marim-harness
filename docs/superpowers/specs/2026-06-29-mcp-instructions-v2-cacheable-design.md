# MCP server instructions on discovery — V2 (cacheable)

**Status:** approved (design)
**Date:** 2026-06-29
**Supersedes the V1 delivery mechanism of:** `2026-06-29-mcp-instructions-on-discovery-design.md` (the feature behaviour is unchanged; only *how* the instructions reach the model changes).

## Problem

V1 surfaces a discovered MCP server's `instructions` via a dynamic `@agent.instructions`
closure (`_discovered_instructions`). Pydantic AI marks `@agent.instructions` output
`dynamic=True`, and the provider does **not** cache it — so after discovery the (~2k/server)
instructions are re-processed at full price on **every** request for the rest of the session.

Measured impact (see mddocs *Tool search: token & latency cost*): tool search's recurring
per-turn overhead (catalog + on-discovery instructions) is uncached, while tool-search-**off**'s
big schemas sit in the cached prefix. On a warm cache that asymmetry erodes tool-search-on's
per-turn cost/latency edge. marim *does* cache (it sets `openrouter_cache_messages="5m"` etc. in
`config/openrouter_cost.py`), so the win is realisable on a caching model.

## Goal

Deliver the **same** on-discovery instructions, but from the **cacheable message history**
instead of an uncached dynamic instruction — written once at the discovery moment, **present in
the same turn** the model first uses the discovered tools. Net: instructions become cache-reads
after the first post-discovery request instead of full-price re-processing every turn.

Non-goals: changing *what* text is injected, the 2000-char cap, or the catalog. This is a
delivery-mechanism change only.

## Feasibility (verified against pydantic_ai 1.107)

- A custom `AbstractCapability.before_model_request(ctx, request_context)` fires **after** the
  framework refreshes `ctx.discovered_tool_names` from history
  (`_agent_graph.py:860`) and **before** the model request is built. It can append to
  `request_context.messages`, which is written back into `ctx.state.message_history`
  (`_agent_graph.py:917`) and persists via `result.all_messages()`.
- Same-turn presence holds: on the request right after `search_tools`, `discovered_tool_names`
  is populated, so the capability injects the instructions into *that* request's messages.
- `request_context.messages` must end with a `ModelRequest` (`_agent_graph.py:908`); appending a
  `[ModelResponse(...), ModelRequest(...)]` pair preserves that.
- Caching premise confirmed: marim sets OpenRouter `cache_messages/cache_instructions/
  cache_tool_definitions="5m"`; `@agent.instructions` output is `dynamic=True` → uncached.
  Message history is the cacheable, monotonic-growth prefix.
- Existing capabilities are wired at `harness.py` via `capabilities=[ProcessHistory(...), ...]`;
  a new `AbstractCapability` slots in there. `ToolSearch` declares `position='outermost'`, so a
  default-ordering marim capability runs after it (after discovery refresh) — correct.

## Design

### New component: `DiscoveredInstructionsCapability`

An `AbstractCapability[Deps]` subclass holding a reference to the `McpManager`. Its only method:

```
async def before_model_request(self, ctx, request_context):
    discovered = getattr(ctx, "discovered_tool_names", None) or set()
    if not discovered:
        return request_context
    pairs = self.mcp.discovered_server_instructions(discovered)   # [(server, text)], existing helper
    already = _injected_servers(request_context.messages)         # scan history for markers
    new = [(s, t) for (s, t) in pairs if s not in already]
    for server, text in new:
        request_context.messages.extend(_instruction_messages(server, text))
    return request_context
```

- **Idempotency by history scan, every call** (not an instance `set`). `_injected_servers`
  scans `request_context.messages` for the per-server marker. This is robust to **resume**
  (fresh capability instance) and **compaction** (if the injected message is summarised away,
  its marker goes too, so the server is re-injected and re-cached — self-healing). O(messages),
  cheap.
- `discovered_server_instructions` and the 2000-char cap (via the renderer) are **reused** from
  V1 — unchanged.

### Injected message shape & marker

`_instruction_messages(server, text)` returns:

```
[ ModelResponse(parts=[TextPart(_MARKER(server))]),
  ModelRequest(parts=[UserPromptPart(_envelope(server, text))]) ]
```

- `_MARKER(server)` = a stable sentinel string, e.g. `«mcp-guidance:{server}»`, used by
  `_injected_servers` to detect prior injection. It is the `ModelResponse` text so it reads as a
  brief assistant note and is trivially scannable.
- `_envelope(server, text)` wraps the (capped) instructions in a clear label, e.g.
  `[MCP server "{server}" — usage guidance, follow it for that server's tools]\n{text}`.
  `UserPromptPart` is used because it is the mid-history part all providers accept; the envelope
  makes clear it is server guidance, not a user utterance.
- The pair ends in a `ModelRequest`, satisfying the framework's `messages[-1]` constraint, and
  contains **no tool calls**, so it cannot create an unanswered-`ToolCallPart` resumability hazard.

### Replace V1

Delete the `_discovered_instructions` closure from `runtime/instructions.py`
(`register_instructions`). Keeping it would inject the instructions twice (dynamic system prompt
*and* synthetic history) and keep the uncached copy, defeating the cache win. The pure helpers
(`render_discovered_instructions`, `McpManager.discovered_server_instructions`,
`discovered_instructions_text`) remain — the capability reuses `discovered_server_instructions`;
`discovered_instructions_text` becomes unused and is removed (it existed only for the closure).

## Files touched

- **Create** `src/marim_harness/mcp/discovered_instructions_capability.py` —
  `DiscoveredInstructionsCapability`, `_instruction_messages`, `_injected_servers`, marker/envelope
  helpers. (Kept out of `manager.py`/`catalog.py` to isolate the pydantic-ai capability concern.)
- **Modify** `src/marim_harness/runtime/harness.py` — add the capability to `capabilities=[...]`.
- **Modify** `src/marim_harness/runtime/instructions.py` — remove `_discovered_instructions`
  closure and the now-unused `discovered_instructions_text` import.
- **Modify** `src/marim_harness/mcp/catalog.py` — remove the now-unused `discovered_instructions_text`
  (the renderer + cap constant stay).
- Tests (below).

## Testing

- **Unit (capability):**
  - empty `discovered_tool_names` → messages unchanged.
  - one discovered server with instructions → appends exactly one `[ModelResponse, ModelRequest]`
    pair; the `ModelRequest` ends the list; envelope contains the server name + text; no tool-call
    parts.
  - **idempotent**: calling again with the marker already in messages → no second injection.
  - **self-healing**: marker removed from messages (compaction sim) → re-injects.
  - a discovered server with no/empty instructions → skipped (delegated to the existing helper,
    but assert the capability emits nothing for it).
  - two servers discovered, one already injected → only the other is added.
- **Resumability:** a history containing the synthetic pair survives marim's
  `_repair_unanswered_tool_calls` unchanged (no dangling tool calls introduced).
- **Success criterion — V1 vs V2 on a caching model (sonnet):** an MCP task that discovers mddocs,
  run multi-turn so the cache warms. Compare, master (V1) vs this branch (V2), the per-turn
  uncached input (`input_tokens − cache_read_tokens`) after discovery. V2 should move the
  instructions into the cached region → measurably fewer fresh tokens/turn post-discovery, and the
  model still follows the instructions (behaviour parity — re-run the canary: still obeyed). If V2
  shows no reduction in uncached tokens, it failed regardless of green unit tests.
- CI order: `ruff` → `pyright` → `pytest`.

## Out of scope (YAGNI)

- Caching the **catalog** the same way (a separate, larger change; noted as future).
- Any change to the instructions text, the 2000-char cap, or gating.
- Provider-specific cache-breakpoint tuning (marim relies on OpenRouter's `"5m"` settings).
