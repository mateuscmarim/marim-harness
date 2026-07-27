"""``marim mcp ...`` — add, list, inspect, and remove MCP servers in mcp.json.

Mirrors the ``claude mcp add`` flag surface so docs and muscle memory transfer:
``marim mcp add <name> <command> [args...]`` for stdio, and
``marim mcp add --transport http|sse <name> <url> -H "K: V"`` for remote servers.
"""

import argparse
import json
import sys
from pathlib import Path

from ...mcp.config import (
    add_server,
    global_mcp_config_path,
    project_mcp_config_path,
    read_servers_with_source,
    remove_server,
)


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


def _build_stdio_spec(*, rest: list[str], headers: list[str], envs: list[str]) -> dict:
    """Build the stdio-transport portion of a server spec: command, args, env.
    Raises :class:`SpecError` if headers are given (http/sse only)."""
    if headers:
        raise SpecError("--header is only valid for http/sse transports")
    spec: dict = {"command": rest[0]}
    if rest[1:]:
        spec["args"] = rest[1:]
    if envs:
        spec["env"] = _parse_pairs(envs, "=", "env")
    return spec


def _build_remote_spec(*, transport: str, rest: list[str], headers: list[str],
                       envs: list[str]) -> dict:
    """Build the http/sse-transport portion of a server spec: url, headers, type.
    Raises :class:`SpecError` if env vars are given (stdio only) or extra positional
    arguments follow the url."""
    if envs:
        raise SpecError("--env is only valid for the stdio transport")
    if len(rest) > 1:
        raise SpecError(f"unexpected extra arguments after url: {rest[1:]}")
    spec: dict = {"url": rest[0]}
    if headers:
        spec["headers"] = _parse_pairs(headers, ":", "header")
    if transport == "sse":
        spec["type"] = "sse"
    return spec


def _build_spec(*, transport: str, rest: list[str], headers: list[str],
                envs: list[str], trust: bool) -> dict:
    """Build a server spec dict from parsed CLI pieces. ``rest`` is the positional
    remainder after the name: ``[command, *args]`` for stdio, ``[url]`` for remote.
    Raises :class:`SpecError` on invalid flag/transport combinations."""
    if not rest:
        need = "a command" if transport == "stdio" else "a url"
        raise SpecError(f"missing {need} for transport {transport!r}")
    if transport == "stdio":
        spec = _build_stdio_spec(rest=rest, headers=headers, envs=envs)
    else:  # http or sse
        spec = _build_remote_spec(transport=transport, rest=rest, headers=headers, envs=envs)
    if trust:
        spec["trust"] = True
    return spec


def _scope_path(scope: str, workspace_root: Path) -> Path:
    """Map a ``--scope`` value to its config file. ``user`` -> global; ``project``
    -> the workspace's ``.marim/mcp.json``."""
    if scope == "user":
        return global_mcp_config_path()
    return project_mcp_config_path(workspace_root)


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim mcp", add_help=True)
    # Like ``git -C``: choose the workspace root for project-scoped servers.
    parser.add_argument(
        "-C", "--workspace", default=None, metavar="DIR",
        help="Workspace root for project-scoped servers (default: current directory).",
    )
    sub = parser.add_subparsers(dest="cmd")

    add = sub.add_parser("add", help="Add an MCP server.")
    add.add_argument("name")
    add.add_argument(
        "-t", "--transport", choices=("stdio", "http", "sse"), default="stdio",
        help="Transport (default: stdio).",
    )
    add.add_argument(
        "-s", "--scope", choices=("user", "project"), default="project",
        help="user = global config; project = .marim/mcp.json (default: project).",
    )
    add.add_argument("-H", "--header", action="append", default=[], metavar="NAME: VALUE",
                     help="HTTP header (repeatable; http/sse only).")
    add.add_argument("-e", "--env", action="append", default=[], metavar="KEY=VALUE",
                     help="Environment variable (repeatable; stdio only).")
    add.add_argument("--trust", action="store_true",
                     help="Bypass tool-call approval for this server.")

    lst = sub.add_parser("list", help="List configured MCP servers.")
    lst.add_argument("--json", action="store_true", help="Emit JSON.")

    get = sub.add_parser("get", help="Show one server's configuration.")
    get.add_argument("name")

    rm = sub.add_parser("remove", help="Remove an MCP server.")
    rm.add_argument("name")
    rm.add_argument("-s", "--scope", choices=("user", "project"), default=None,
                    help="Limit removal to one scope (default: search project then user).")
    return parser


def _workspace_root(args) -> Path:
    return Path(args.workspace) if args.workspace else Path.cwd()


def _cmd_add(args, rest, *, out, err) -> int:
    try:
        spec = _build_spec(
            transport=args.transport, rest=rest, headers=args.header,
            envs=args.env, trust=args.trust,
        )
    except SpecError as exc:
        print(f"error: {exc}", file=err)
        return 2
    path = _scope_path(args.scope, _workspace_root(args))
    if not add_server(path, args.name, spec):
        print(f"error: server {args.name!r} already exists in {path} "
              f"(remove it first, or pick another name)", file=err)
        return 1
    print(f"Added MCP server {args.name!r} ({args.transport}) to {path}", file=out)
    if args.scope == "project":
        print("note: project servers in .marim/mcp.json load only when the project "
              "is trusted ('marim trust grant' or MARIM_TRUST_PROJECT_HOOKS).", file=err)
    return 0


def _cmd_list(args, *, out, err) -> int:
    servers = read_servers_with_source(_workspace_root(args))
    if args.json:
        print(json.dumps({n: {"source": s, **spec} for n, (spec, s) in servers.items()}), file=out)
        return 0
    if not servers:
        print("No MCP servers configured.", file=out)
        return 0
    for name, (spec, source) in sorted(servers.items()):
        target = spec.get("command") or spec.get("url") or "?"
        disabled = "  (disabled)" if spec.get("enabled") is False else ""
        print(f"{name}  [{source}]  {target}{disabled}", file=out)
    return 0


def _cmd_get(args, *, out, err) -> int:
    servers = read_servers_with_source(_workspace_root(args))
    entry = servers.get(args.name)
    if entry is None:
        print(f"error: no MCP server named {args.name!r}", file=err)
        return 1
    spec, source = entry
    print(f"{args.name}  [{source}]", file=out)
    print(json.dumps(spec, indent=2), file=out)
    return 0


def _cmd_remove(args, *, out, err) -> int:
    workspace_root = _workspace_root(args)
    scopes = [args.scope] if args.scope else ["project", "user"]
    for scope in scopes:
        path = _scope_path(scope, workspace_root)
        if remove_server(path, args.name):
            print(f"Removed MCP server {args.name!r} from {path}", file=out)
            return 0
    print(f"error: no MCP server named {args.name!r} to remove", file=err)
    return 1


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = _build_parser()
    # ``add`` accepts a positional remainder (command + args, or url) that may
    # contain dashes; parse_known_args pulls the recognized options out wherever
    # they appear and leaves the rest in order.
    # ``name`` is a declared positional on each subparser; parse_known_args leaves
    # the ``add`` spec positionals (command + args, or url) in ``rest`` — they may
    # contain dashes, so they cannot be declared as a fixed positional.
    args, rest = parser.parse_known_args(argv)
    if args.cmd == "add":
        return _cmd_add(args, rest, out=out, err=err)
    if rest:
        print(f"error: unexpected arguments: {rest}", file=err)
        return 2
    if args.cmd == "list":
        return _cmd_list(args, out=out, err=err)
    if args.cmd == "get":
        return _cmd_get(args, out=out, err=err)
    if args.cmd == "remove":
        return _cmd_remove(args, out=out, err=err)
    parser.print_help(err)
    return 2
