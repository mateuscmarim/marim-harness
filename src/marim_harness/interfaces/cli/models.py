"""`marim models ...` — list available models."""

import argparse
import asyncio
import json
import sys

from ...config import MultiModelSource


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim models", add_help=True)
    sub = parser.add_subparsers(dest="cmd")

    lst = sub.add_parser("list", help="List available models for the provider.")
    lst.add_argument("--json", action="store_true", help="Emit JSON.")

    return parser


def _cmd_list(args, *, out, err) -> int:
    source = MultiModelSource.from_env()
    entries = asyncio.run(source.list_models())

    if args.json:
        print(json.dumps([{"id": e.id, "name": e.name, "provider": e.provider}
                          for e in entries]), file=out)
        return 0

    if not entries:
        print(
            "No models available from the configured providers. Check your credentials"
            " / MARIM_BASE_URL, or set MARIM_MODEL to a model id directly.",
            file=out,
        )
        return 0

    width = max((len(e.qualified) for e in entries), default=0)
    for e in entries:
        print(f"{e.qualified.ljust(width)}  {e.name}", file=out)
    return 0


def main(argv: list[str], *, out=sys.stdout, err=sys.stderr) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.cmd == "list":
        return _cmd_list(args, out=out, err=err)
    parser.print_help(err)
    return 2
