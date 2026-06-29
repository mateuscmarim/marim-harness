"""The tool-search discovery catalog: a server-grouped list of deferred MCP tool
names injected into the prompt so the model knows what it can discover via
``search_tools`` (the schemas stay deferred). See the tool-catalog design doc."""

from .manager import should_defer

# At most this many tool names per server in the catalog; the rest collapse to a
# "(+N more)" hint. Names-only is cheap, but a server with dozens of tools would
# still bloat the prefix — and 12 names is ample query vocabulary for one server.
_CATALOG_PER_SERVER_CAP = 12

_CATALOG_PREAMBLE = (
    "Additional MCP tools are available but not loaded by default. Use the "
    "search_tools function to discover and load them (query with words from the "
    "names below) before concluding a capability is unavailable. Available tools "
    "by server:"
)


def render_tool_catalog(groups: dict[str, list[str]]) -> str:
    """Render a deterministic, server-grouped catalog of deferred tool names. Shows
    at most ``_CATALOG_PER_SERVER_CAP`` names per server, then ``(+N more)``. Servers
    are sorted for byte-stable output (cache-friendly); names are rendered in the
    order given (the caller pre-sorts them). Empty string when there are no groups."""
    if not groups:
        return ""
    lines = [_CATALOG_PREAMBLE]
    for server in sorted(groups):
        names = groups[server]
        shown = names[:_CATALOG_PER_SERVER_CAP]
        extra = len(names) - len(shown)
        suffix = f" (+{extra} more)" if extra > 0 else ""
        lines.append(f"- {server}: {', '.join(shown)}{suffix}")
    return "\n".join(lines)


async def tool_catalog_text(mcp, policy: str, threshold: int) -> str:
    """The catalog block to inject when tool search is deferring this run, else "".
    Gated by the same ``should_defer`` the controller uses for ``toolsets_for``, so
    the catalog is shown exactly when the MCP tools are actually deferred. ``mcp`` is
    an ``McpManager`` (duck-typed: needs ``async live_tools_by_server()``)."""
    groups = await mcp.live_tools_by_server()
    total = sum(len(v) for v in groups.values())
    if not should_defer(policy, total, threshold):
        return ""
    return render_tool_catalog(groups)


# At most this many chars of a single server's instructions go into the prompt;
# beyond that we clip with a marker. Server instructions can be long and the
# discovered-instructions capability injects them into cacheable history (once per
# server per session), so the cap bounds the fixed cache cost.
_INSTRUCTIONS_CAP = 2000


def cap_instructions(text: str) -> str:
    """Clip one server's instructions to ``_INSTRUCTIONS_CAP`` chars with a truncation
    marker — the per-server bound shared by the discovered-instructions delivery."""
    body = text.strip()
    if len(body) > _INSTRUCTIONS_CAP:
        body = body[:_INSTRUCTIONS_CAP].rstrip() + "\n…(truncated)"
    return body
