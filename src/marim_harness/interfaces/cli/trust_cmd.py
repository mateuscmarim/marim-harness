"""`marim trust` — inspect or set the per-project trust decision from the
command line: the headless/CI counterpart of the TUI's first-open dialog
(headless runs never prompt; they honor what this records)."""

import argparse
import sys
from datetime import datetime, timezone
from pathlib import Path

from ...trust import record_decision, resolve_project_trust, trust_env
from ...trust_surface import scan_project_surface


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="marim trust", add_help=True)
    parser.add_argument(
        "action", nargs="?", default="status", choices=["status", "grant", "revoke"],
        help="status (default): show the resolved decision. grant/revoke: persist one.",
    )
    parser.add_argument("workspace", nargs="?", default=".", help="Project root (default: cwd).")
    return parser


def _cmd_status(root: Path, *, out) -> int:
    surface = scan_project_surface(root)
    resolution = resolve_project_trust(
        root, explicit=None, fingerprint=surface.fingerprint, surface_empty=surface.empty
    )
    state = "trusted" if resolution.trusted else "untrusted"
    print(f"{root}: {state} (source: {resolution.source})", file=out)
    print(f"gated project config — {surface.summary()}", file=out)
    if trust_env() is not None:
        print("note: MARIM_TRUST_PROJECT_HOOKS is set and overrides the store", file=out)
    return 0


def _cmd_decide(root: Path, action: str, *, out) -> int:
    surface = scan_project_surface(root)
    record_decision(
        root,
        trusted=action == "grant",
        fingerprint=surface.fingerprint,
        now=datetime.now(timezone.utc).isoformat(),
    )
    verb = "granted" if action == "grant" else "revoked"
    print(f"trust {verb} for {root}", file=out)
    if action == "grant":
        print(f"will load — {surface.summary()}", file=out)
    return 0


def main(argv: list[str], *, out=None, err=None) -> int:
    # `out`/`err` default to None (resolved to the *current* sys.stdout/stderr
    # inside the call) rather than binding the stream object at def-time: a
    # `def main(..., out=sys.stdout)` default is evaluated once, at first
    # import, and this module can get imported (caching the stale binding) by
    # whichever test happens to touch it first in a given process — before
    # pytest's capsys has swapped sys.stdout in for the test that actually
    # asserts on captured output. Resolving here instead means every call
    # picks up whatever is currently installed.
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=err)
        return 2
    if args.action == "status":
        return _cmd_status(root, out=out)
    return _cmd_decide(root, args.action, out=out)


# The tests (and any script driving this module directly) call `run` rather
# than `main` — kept as a plain alias so both spellings work: `main` matches
# the out=/err=-taking convention every other `interfaces/cli/*.py` command
# group uses (see config.py/models.py) and is what router.py dispatches to;
# `run` is the name this command's own brief/tests were written against.
run = main
