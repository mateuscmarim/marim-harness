"""``marim plugin ...`` — install and manage plugins."""

import argparse
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...plugins import (
    InstallError,
    ManifestError,
    discover_plugins,
    has_executable,
    install_plugin,
    load_manifest,
    plugin_bundle_summary,
    remove_plugin,
    set_enabled,
    set_trusted,
    update_plugin,
)


def _now() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim plugin", add_help=True)
    # Like ``git -C``: pick the workspace root for project-scoped plugins instead
    # of the cwd. Global plugins are found regardless. Must precede the subcommand.
    parser.add_argument(
        "-C", "--workspace", default=None, metavar="DIR",
        help="Workspace root for project-scoped plugins (default: current directory).",
    )
    sub = parser.add_subparsers(dest="cmd")

    inst = sub.add_parser("install", help="Install a plugin from a path or git URL.")
    inst.add_argument("source")
    inst.add_argument("--scope", choices=("global", "project"), default="global")
    inst.add_argument("--trust", action="store_true", help="Trust executable parts (hooks/MCP).")
    inst.add_argument(
        "--link", action="store_true", help="Symlink a local source instead of copying."
    )
    inst.add_argument("--name", default=None, help="Override the installed name.")

    lst = sub.add_parser("list", help="List installed plugins.")
    lst.add_argument("--json", action="store_true")

    for name, help_ in (
        ("info", "Show one plugin's details."),
        ("enable", "Enable a plugin."),
        ("disable", "Disable a plugin."),
        ("trust", "Trust a plugin's executable parts."),
        ("remove", "Uninstall a plugin."),
        ("update", "Re-fetch a git-sourced plugin."),
    ):
        p = sub.add_parser(name, help=help_)
        p.add_argument("name")
        p.add_argument("--scope", choices=("global", "project"), default="global")

    val = sub.add_parser("validate", help="Validate a plugin directory's manifest.")
    val.add_argument("path")
    return parser


def _scope_of(name, workspace_root, preferred):
    """Find which scope a plugin is installed in, preferring ``preferred``."""
    for p in discover_plugins(workspace_root):
        if p.name == name and (preferred is None or p.scope == preferred):
            return p.scope
    for p in discover_plugins(workspace_root):
        if p.name == name:
            return p.scope
    return None


def _cmd_install(args, *, ws, out, err, input_fn, now_fn) -> int:
    source = args.source
    trust = args.trust
    # If executable and trust not pre-granted, prompt (interactive only).
    if not trust:
        manifest_dir = Path(source)
        try:
            manifest = load_manifest(manifest_dir) if manifest_dir.is_dir() else None
        except ManifestError:
            manifest = None
        if manifest is not None:
            summary = plugin_bundle_summary(manifest)
            if has_executable(summary):
                print(
                    f"Plugin {manifest.name!r} bundles "
                    f"{summary['skills']} skills, {summary['agents']} agents, "
                    f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers.",
                    file=out,
                )
                answer = input_fn("Trust this plugin's hooks/MCP servers? [y/N] ").strip().lower()
                trust = answer in ("y", "yes")
    try:
        rec = install_plugin(
            source,
            scope=args.scope,
            workspace_root=ws,
            trust=trust,
            link=args.link,
            name_override=args.name,
            now=now_fn(),
        )
    except InstallError as exc:
        print(f"error: {exc}", file=err)
        return 1
    state = "trusted" if rec.trusted else "untrusted"
    print(
        f"installed {rec.name} ({rec.version or 'unknown'}) [{args.scope}, {state}]",
        file=out,
    )
    return 0


def _cmd_list(args, *, ws, out, err) -> int:
    plugins = discover_plugins(ws)
    if args.json:
        print(
            json.dumps([
                {
                    "name": p.name,
                    "scope": p.scope,
                    "version": p.record.version,
                    "enabled": p.record.enabled,
                    "trusted": p.record.trusted,
                }
                for p in plugins
            ]),
            file=out,
        )
        return 0
    if not plugins:
        print("no plugins installed", file=out)
        return 0
    for p in plugins:
        flags = "enabled" if p.record.enabled else "disabled"
        flags += ", trusted" if p.record.trusted else ", untrusted"
        print(f"{p.name}  [{p.scope}, {flags}]  {p.manifest.description}", file=out)
    return 0


def _cmd_info(args, *, ws, out, err) -> int:
    for p in discover_plugins(ws):
        if p.name == args.name:
            summary = plugin_bundle_summary(p.manifest)
            print(f"name:        {p.name}", file=out)
            print(f"version:     {p.record.version or 'unknown'}", file=out)
            print(f"scope:       {p.scope}", file=out)
            print(f"enabled:     {p.record.enabled}", file=out)
            print(f"trusted:     {p.record.trusted}", file=out)
            print(f"description: {p.manifest.description}", file=out)
            print(
                f"bundles:     {summary['skills']} skills, {summary['agents']} agents, "
                f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers",
                file=out,
            )
            print(f"source:      {p.record.source}", file=out)
            return 0
    print(f"error: plugin not found: {args.name}", file=err)
    return 1


def _cmd_toggle(args, *, ws, out, err, action) -> int:
    scope = _scope_of(args.name, ws, args.scope)
    if scope is None:
        print(f"error: plugin not found: {args.name}", file=err)
        return 1
    ok = action(scope)
    if not ok:
        print(f"error: could not update {args.name}", file=err)
        return 1
    print(f"{args.name}: ok", file=out)
    return 0


def _cmd_validate(args, *, out, err) -> int:
    try:
        manifest = load_manifest(Path(args.path))
    except ManifestError as exc:
        print(f"invalid: {exc}", file=err)
        return 1
    summary = plugin_bundle_summary(manifest)
    print(
        f"valid: {manifest.name} ({manifest.version or 'unknown'}) — "
        f"{summary['skills']} skills, {summary['agents']} agents, "
        f"{summary['hooks']} hooks, {summary['mcpServers']} MCP servers",
        file=out,
    )
    return 0


def _resolve_workspace(args, *, err) -> Path | None:
    """Resolve ``--workspace`` to an absolute directory. Prints an error and
    returns ``None`` if the given path isn't a directory (caller exits 2)."""
    if args.workspace is None:
        return Path.cwd()
    ws = Path(args.workspace)
    if not ws.is_dir():
        print(f"error: not a directory: {args.workspace}", file=err)
        return None
    return ws.resolve()


def _toggle_action(cmd: str, args, ws: Path):
    """Build the scope-taking mutation callable for the enable/disable/trust/
    remove subcommands, so ``main`` can route them through one ``_cmd_toggle``
    call instead of one branch per verb."""
    if cmd == "enable":
        return lambda s: set_enabled(args.name, scope=s, workspace_root=ws, enabled=True)
    if cmd == "disable":
        return lambda s: set_enabled(args.name, scope=s, workspace_root=ws, enabled=False)
    if cmd == "trust":
        return lambda s: set_trusted(args.name, scope=s, workspace_root=ws, trusted=True)
    return lambda s: remove_plugin(args.name, scope=s, workspace_root=ws)


def _cmd_update(args, *, ws, out, err, now_fn) -> int:
    scope = _scope_of(args.name, ws, args.scope)
    if scope is None:
        print(f"error: plugin not found: {args.name}", file=err)
        return 1
    try:
        rec = update_plugin(args.name, scope=scope, workspace_root=ws, now=now_fn())
    except InstallError as exc:
        print(f"error: {exc}", file=err)
        return 1
    print(f"updated {rec.name} to {rec.version or 'unknown'}", file=out)
    return 0


def main(
    argv: list[str],
    *,
    out=sys.stdout,
    err=sys.stderr,
    input_fn=input,
    now_fn=_now,
) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    ws = _resolve_workspace(args, err=err)
    if ws is None:
        return 2
    if args.cmd == "install":
        return _cmd_install(args, ws=ws, out=out, err=err, input_fn=input_fn, now_fn=now_fn)
    if args.cmd == "list":
        return _cmd_list(args, ws=ws, out=out, err=err)
    if args.cmd == "info":
        return _cmd_info(args, ws=ws, out=out, err=err)
    if args.cmd in ("enable", "disable", "trust", "remove"):
        return _cmd_toggle(
            args, ws=ws, out=out, err=err, action=_toggle_action(args.cmd, args, ws)
        )
    if args.cmd == "update":
        return _cmd_update(args, ws=ws, out=out, err=err, now_fn=now_fn)
    if args.cmd == "validate":
        return _cmd_validate(args, out=out, err=err)
    parser.print_help(err)
    return 2
