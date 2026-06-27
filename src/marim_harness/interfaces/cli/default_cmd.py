"""The default invocation (no management subcommand): launch the interactive
TUI, or — when ``-p/--print`` is given or stdin is piped — run a single turn
headlessly and print the result."""

import argparse
import asyncio
import sys
from pathlib import Path

from ...history import PromptHistory, default_history_path


def _version() -> str:
    """The installed package version, or a placeholder when running from a source
    tree that was never installed (no dist metadata)."""
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("marim-harness")
    except PackageNotFoundError:
        return "unknown"


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="marim")
    p.add_argument(
        "--version", action="version", version=f"%(prog)s {_version()}",
    )
    p.add_argument(
        "workspace", nargs="?", default=None,
        help="workspace directory (defaults to the current directory)",
    )
    p.add_argument(
        "--resume", action="store_true",
        help="resume the saved conversation for this workspace",
    )
    p.add_argument(
        "-p", "--print", nargs="?", const=True, default=None, dest="prompt",
        metavar="PROMPT",
        help="run headlessly: PROMPT, or read the prompt from stdin if omitted",
    )
    p.add_argument(
        "--output-format", choices=["text", "json", "stream-json"], default="text",
        dest="output_format", help="headless output format (default: text)",
    )
    p.add_argument(
        "--mode", choices=["plan", "auto"], default=None,
        help="headless permission mode (default: auto). 'ask' needs the TUI",
    )
    p.add_argument(
        "--worktree", metavar="BRANCH", default=None,
        help="run inside a git worktree for BRANCH under <repo>/.worktrees/, "
             "creating it (from current HEAD) or reusing it",
    )
    return p


def _is_headless(prompt, *, stdin_isatty: bool) -> bool:
    """Headless when an explicit prompt/flag was given, or stdin is piped."""
    return prompt is not None or not stdin_isatty


def _enter_worktree(workspace, branch, err):
    """Resolve `workspace` to a git worktree for `branch`. Returns the worktree
    path, or None after printing an error to `err`."""
    from ...workspace.worktree import (
        WorktreeError,
        create_or_reuse_worktree,
        repo_root,
    )

    root = repo_root(workspace)
    if root is None:
        print(f"--worktree: {workspace} is not a git repository", file=err)
        return None
    try:
        return create_or_reuse_worktree(root, branch)
    except WorktreeError as exc:
        print(f"--worktree: {exc}", file=err)
        return None


def run_default(argv, *, stdin=None, out=None, err=None) -> int:
    stdin = stdin if stdin is not None else sys.stdin
    out = out if out is not None else sys.stdout
    err = err if err is not None else sys.stderr

    args = _build_parser().parse_args(argv)
    workspace = Path(args.workspace).resolve() if args.workspace else Path.cwd()

    if args.worktree:
        workspace = _enter_worktree(workspace, args.worktree, err)
        if workspace is None:
            return 2

    # Heavy imports (pydantic_ai) deferred to here so `--help` and arg errors stay
    # fast; only an actual launch pays for the agent.
    from ...runtime.bootstrap import build_harness
    from ...runtime.permissions import Mode

    if _is_headless(args.prompt, stdin_isatty=stdin.isatty()):
        prompt = args.prompt if isinstance(args.prompt, str) else stdin.read()
        prompt = (prompt or "").strip()
        if not prompt:
            print("no prompt provided", file=err)
            return 2
        from .headless import run_headless

        mode = Mode(args.mode) if args.mode else Mode.auto
        harness = build_harness(workspace, mode=mode, resume=args.resume)
        return asyncio.run(
            run_headless(harness, prompt, args.output_format, out=out, err=err)
        )

    from ..tui.app import HarnessApp

    harness = build_harness(workspace, mode=Mode.ask, resume=args.resume)
    HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    return 0
