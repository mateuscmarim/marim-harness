"""Per-turn toolset composition for the main agent.

Folds the live MCP toolsets and the (optional) LSP toolset into the single list
passed to ``agent.run(toolsets=…)`` each turn, under ONE tool-search deferral
decision. LSP thus shares the MCP budget: below threshold it rides inline; above
it, MCP and LSP defer together behind one ToolSearch (riding an already-present
ToolSearch at ~zero marginal cost). Keeping this here — not on ``McpManager`` —
means the MCP manager never learns about LSP, and the controller stays thin.

With ``lsp_toolset=None`` this yields just the live MCP toolsets — inline, or a
single deferred+combined toolset above threshold — so disabling LSP tools is a
no-op on the toolset path. (This is the sole per-turn composition; it subsumed
the former ``McpManager.toolsets_for``.)

Why tool-level deferral (``DeferredLoadingToolset`` + the auto-injected
ToolSearch capability) and not pydantic-ai 2.x's capability-level
``defer_loading=True`` (the ``load_capability`` catalog), evaluated during the
v2 migration (2026-07):

- Granularity. ``load_capability`` loads a whole capability at once — for a
  40-tool MCP server, all 40 schemas land in context. Tool search discovers
  individual tools by keyword, which is the point of deferring: keep the
  surface proportional to what the turn actually needs.
- The threshold policy is product surface, not plumbing. ``should_defer``'s
  on/auto/off × threshold knob (MARIM_TOOL_SEARCH*, settings screen) decides
  WHETHER to defer at all; capability ``defer_loading`` is a per-capability
  bool with no equivalent, so adopting it would keep should_defer anyway and
  merely swap the mechanism underneath.
- Churn without payoff. The load_capability flow introduces typed
  ``tool_kind='capability-load'`` parts into history (session round-trip, TUI
  stream rendering, hooks dispatch all see a new part kind) and would replace
  three working, tested subsystems — the prompt tool catalog
  (``mcp/catalog.py``), discovered-server-instructions injection, and the
  sub-agent grant deferral (``McpManager.granted_toolsets``) — with coarser
  equivalents.
Revisit if upstream grows per-tool loading or threshold semantics on
capability deferral.
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
    # Compose is where the model-facing <name>_<tool> shape appears: the manager
    # holds raw MCPToolsets (one stable handle per server for lifecycle and
    # introspection) and .prefixed(name) wraps them per turn.
    live = [s.prefixed(mcp.server_name(s)) for s in mcp.live_toolsets()]
    has_lsp = lsp_toolset is not None
    extras = [lsp_toolset] if has_lsp else []
    combined = [*live, *extras]
    if not combined:
        return []
    count = await mcp.live_tool_count() + (lsp_count if has_lsp else 0)
    if should_defer(policy, count, threshold):
        return [DeferredLoadingToolset(CombinedToolset(combined))]
    return combined
