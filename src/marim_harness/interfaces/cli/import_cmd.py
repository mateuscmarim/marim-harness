"""`marim import claude` — carry an existing Claude Code CLI setup into marim.

Today this imports exactly one thing: the *memory store*. marim's memory format
deliberately mirrors Claude Code's, so an importer is how that promise stays
checkable — format drift shows up here as an import gap rather than as a
surprise on switching day. Skills, sub-agents, hooks and MCP servers are
separate slices with their own trust questions and are not handled here.

The module is thin wiring: every format decision lives in
``workspace.claude_import``.
"""

import argparse
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path

from ...workspace.claude_import import (
    PlannedImport,
    SourceScan,
    apply_plan,
    claude_config_dir,
    claude_memory_dir,
    plan_import,
    read_source,
    target_state,
)
from ...workspace.memory import project_scope


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="marim import",
        description="Import an existing Claude Code setup into this workspace.",
    )
    parser.add_argument(
        "source", choices=["claude"], help="What to import from. Only `claude` today."
    )
    parser.add_argument("workspace", nargs="?", default=".", help="Project root (default: cwd).")
    parser.add_argument(
        "--from", dest="from_dir", default=None,
        help="Claude memory dir (or the project dir containing it), skipping auto-detection.",
    )
    parser.add_argument(
        "--apply", action="store_true",
        help="Perform the import. Without this the command only reports what it would do.",
    )
    parser.add_argument(
        "--force", action="store_true",
        help="Overwrite marim memories that conflict with an imported one.",
    )
    return parser


def _candidate_dirs(config_dir: Path) -> list[tuple[Path, int]]:
    """Every Claude project memory dir that exists, with its memory count, so a
    failed auto-detection can tell the user what `--from` could point at."""
    projects = config_dir / "projects"
    found: list[tuple[Path, int]] = []
    if not projects.is_dir():
        return found
    for entry in sorted(projects.iterdir()):
        memdir = entry / "memory"
        if memdir.is_dir():
            count = len([p for p in memdir.glob("*.md") if p.name != "MEMORY.md"])
            found.append((memdir, count))
    return found


def _resolve_source(from_dir: str | None, root: Path, *, err) -> Path | None:
    """The Claude memory dir to read, or ``None`` after printing why not.

    ``--from`` accepts either the memory dir itself or the project dir holding
    it, since the two are easy to confuse when copying a path off a listing.
    """
    if from_dir is not None:
        given = Path(from_dir).expanduser().resolve()
        candidate = given / "memory" if (given / "memory").is_dir() else given
        if not candidate.is_dir():
            print(f"not a directory: {given}", file=err)
            return None
        return candidate
    memdir = claude_memory_dir(root, config_dir=claude_config_dir())
    if memdir.is_dir():
        return memdir
    print(f"no Claude Code memory found for {root}", file=err)
    print(f"  looked in: {memdir}", file=err)
    candidates = _candidate_dirs(claude_config_dir())
    if candidates:
        print("  available stores — re-run with --from <path>:", file=err)
        for path, count in candidates:
            print(f"    {path}  ({count} memories)", file=err)
    return None


def _repo_tracks_target(root: Path) -> bool:
    """Whether writing into ``<root>/.marim`` would land in git-tracked space.

    Only a *definitive* "not ignored" counts. A missing git binary, a
    non-repo, or any unexpected return code means we cannot tell — and a
    warning printed on every run when we cannot tell is worse than silence.
    """
    if not (root / ".git").exists():
        return False
    try:
        completed = subprocess.run(
            ["git", "-C", str(root), "check-ignore", "-q", ".marim/"],
            capture_output=True,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return completed.returncode == 1  # 0 = ignored, 1 = not ignored, other = unknown


def _report_scan(scan: SourceScan, source: Path, target: Path, *, out, err) -> None:
    print(f"source: {source}  ({len(scan.memories)} memories)", file=out)
    print(f"target: {target}", file=out)
    print("", file=out)
    for problem in scan.problems:
        print(f"  source problem — {problem}", file=err)


def _report_plan(plan: Sequence[PlannedImport], *, out) -> None:
    width = max((len(entry.slug) for entry in plan), default=0)
    for entry in plan:
        detail = entry.reason or entry.title
        print(f"  {entry.action:<9} {entry.slug:<{width}}  {detail}", file=out)
    if plan:
        print("", file=out)


def main(argv: list[str], *, out=None, err=None) -> int:
    # `out`/`err` resolve to the *current* streams inside the call rather than
    # being bound at def-time, so pytest's capsys sees this module's output no
    # matter which test imported it first. Same reasoning as trust_cmd.main.
    out = sys.stdout if out is None else out
    err = sys.stderr if err is None else err
    args = _build_parser().parse_args(argv)

    root = Path(args.workspace).resolve()
    if not root.is_dir():
        print(f"not a directory: {root}", file=err)
        return 2

    source = _resolve_source(args.from_dir, root, err=err)
    if source is None:
        return 1

    scan = read_source(source)
    scope = project_scope(root)
    _report_scan(scan, source, scope.root, out=out, err=err)
    if not scan.memories:
        print("nothing to import.", file=out)
        return 0

    existing_slugs, existing_titles = target_state(scope)
    plan = plan_import(
        scan.memories,
        existing_slugs=existing_slugs,
        existing_titles=existing_titles,
        force=args.force,
    )
    _report_plan(plan, out=out)

    if not args.apply:
        pending = sum(1 for entry in plan if entry.action != "skip")
        skipped = len(plan) - pending
        print(f"{pending} to import, {skipped} skipped.", file=out)
        print("Dry run — re-run with --apply to write.", file=out)
        return 0

    if _repo_tracks_target(root):
        print(
            f"warning: {scope.root} is inside a git repo and is not gitignored — "
            "imported memories would be committable.",
            file=err,
        )
    result = apply_plan(plan, scan.memories, scope)
    for slug in result.failed:
        print(f"failed to write memory {slug!r}", file=err)
    print(
        f"{len(result.imported)} imported, {len(result.skipped)} skipped"
        + (f", {len(result.failed)} failed" if result.failed else "")
        + ".",
        file=out,
    )
    return 1 if result.failed else 0


# `run` is the spelling this command's tests were written against; `main` is
# what router.py dispatches to and what every other cli/* module exposes. Same
# alias arrangement as trust_cmd.
run = main
