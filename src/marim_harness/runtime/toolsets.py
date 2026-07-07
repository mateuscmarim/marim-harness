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
    has_lsp = lsp_toolset is not None
    extras = [lsp_toolset] if has_lsp else []
    combined = [*live, *extras]
    if not combined:
        return []
    count = await mcp.live_tool_count() + (lsp_count if has_lsp else 0)
    if should_defer(policy, count, threshold):
        return [DeferredLoadingToolset(CombinedToolset(combined))]
    return combined
