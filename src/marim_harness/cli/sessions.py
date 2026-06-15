"""`marim sessions ...` — list and manage saved sessions for a workspace."""

import argparse
import json
import sys
from pathlib import Path

from ..session import SessionManager

_COLUMNS = ("ID", "NAME", "UPDATED", "MESSAGES", "TOKENS")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marim sessions",
        description="List and manage saved sessions.",
    )
    sub = parser.add_subparsers(dest="command")

    p_list = sub.add_parser("list", help="list saved sessions for a workspace")
    p_list.add_argument(
        "workspace", nargs="?", default=".",
        help="workspace directory (default: current directory)",
    )
    p_list.add_argument("--json", action="store_true", help="emit JSON instead of a table")

    p_delete = sub.add_parser("delete", help="delete a saved session by id")
    p_delete.add_argument("id", help="id of the session to delete")
    p_delete.add_argument(
        "workspace", nargs="?", default=".",
        help="workspace directory (default: current directory)",
    )

    return parser


def _manager(workspace: str) -> SessionManager:
    return SessionManager(Path(workspace).resolve())


def _cmd_list(args, *, out, err) -> int:
    infos = _manager(args.workspace).list()

    if args.json:
        payload = [
            {
                "id": info.id,
                "name": info.name,
                "updated": info.updated,
                "message_count": info.message_count,
                "tokens": info.tokens,
            }
            for info in infos
        ]
        print(json.dumps(payload), file=out)
        return 0

    if not infos:
        print("No sessions saved for this workspace.", file=out)
        return 0

    rows = [
        (info.id, info.name, info.updated, str(info.message_count), str(info.tokens))
        for info in infos
    ]
    widths = [
        max(len(_COLUMNS[i]), *(len(row[i]) for row in rows))
        for i in range(len(_COLUMNS))
    ]
    for cells in (_COLUMNS, *rows):
        print("  ".join(cell.ljust(widths[i]) for i, cell in enumerate(cells)), file=out)
    return 0


def _cmd_delete(args, *, out, err) -> int:
    manager = _manager(args.workspace)
    ids = {info.id for info in manager.list()}
    if args.id not in ids:
        print(f"No session with id {args.id!r} in this workspace.", file=err)
        return 1
    manager.delete(args.id)
    print(f"Deleted session {args.id}.", file=out)
    return 0


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)

    if args.command == "list":
        return _cmd_list(args, out=out, err=err)
    if args.command == "delete":
        return _cmd_delete(args, out=out, err=err)

    parser.print_help(err)
    return 2
