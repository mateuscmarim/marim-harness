"""``marim mcp ...`` — add, list, inspect, and remove MCP servers in mcp.json.

Mirrors the ``claude mcp add`` flag surface so docs and muscle memory transfer:
``marim mcp add <name> <command> [args...]`` for stdio, and
``marim mcp add --transport http|sse <name> <url> -H "K: V"`` for remote servers.
"""


class SpecError(ValueError):
    """A user-facing validation failure while building a server spec."""


def _parse_pairs(items: list[str], sep: str, what: str) -> dict[str, str]:
    """Split ``["K<sep>V", ...]`` into a dict, trimming whitespace around the value.
    Raises :class:`SpecError` naming ``what`` if any token lacks ``sep``."""
    out: dict[str, str] = {}
    for item in items:
        if sep not in item:
            raise SpecError(f"invalid {what} {item!r}: expected 'KEY{sep}VALUE'")
        key, value = item.split(sep, 1)
        out[key.strip()] = value.strip()
    return out


def _build_spec(*, transport: str, rest: list[str], headers: list[str],
                envs: list[str], trust: bool) -> dict:
    """Build a server spec dict from parsed CLI pieces. ``rest`` is the positional
    remainder after the name: ``[command, *args]`` for stdio, ``[url]`` for remote.
    Raises :class:`SpecError` on invalid flag/transport combinations."""
    if not rest:
        need = "a command" if transport == "stdio" else "a url"
        raise SpecError(f"missing {need} for transport {transport!r}")
    spec: dict = {}
    if transport == "stdio":
        if headers:
            raise SpecError("--header is only valid for http/sse transports")
        spec["command"] = rest[0]
        if rest[1:]:
            spec["args"] = rest[1:]
        if envs:
            spec["env"] = _parse_pairs(envs, "=", "env")
    else:  # http or sse
        if envs:
            raise SpecError("--env is only valid for the stdio transport")
        if len(rest) > 1:
            raise SpecError(f"unexpected extra arguments after url: {rest[1:]}")
        spec["url"] = rest[0]
        if headers:
            spec["headers"] = _parse_pairs(headers, ":", "header")
        if transport == "sse":
            spec["type"] = "sse"
    if trust:
        spec["trust"] = True
    return spec
