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
# beyond that we clip with a marker. Server instructions can be long, and this
# block is re-sent each turn after discovery (dynamic instructions aren't cached),
# so the cap bounds the recurring cost — mirrors the catalog's per-server name cap.
_INSTRUCTIONS_CAP = 2000

_DISCOVERED_PREAMBLE = (
    "Usage guidance for the MCP servers you've loaded (follow it for those tools):"
)


def discovered_instructions_text(mcp, discovered: set[str]) -> str:
    """The usage-guidance block to inject for servers the model has discovered this
    run, or "" when nothing has been discovered. ``mcp`` is an ``McpManager``
    (duck-typed: needs ``discovered_server_instructions``)."""
    if not discovered:
        return ""
    return render_discovered_instructions(mcp.discovered_server_instructions(discovered))


def render_discovered_instructions(servers: list[tuple[str, str]]) -> str:
    """Render a deterministic block of server-authored usage instructions for
    servers the model has discovered. ``servers`` is ``(server_name, instructions)``
    pairs (already filtered to non-empty). Each server's text is clipped to
    ``_INSTRUCTIONS_CAP`` chars with a ``…(truncated)`` marker. Servers are sorted
    for byte-stable output. Empty string when there are no servers."""
    if not servers:
        return ""
    lines = [_DISCOVERED_PREAMBLE]
    for name, text in sorted(servers):
        body = text.strip()
        if len(body) > _INSTRUCTIONS_CAP:
            body = body[:_INSTRUCTIONS_CAP].rstrip() + "\n…(truncated)"
        lines.append(f"\n## {name}\n{body}")
    return "\n".join(lines)
