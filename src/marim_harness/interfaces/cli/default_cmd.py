"""The default invocation (no management subcommand): launch the interactive
TUI, or — when ``-p/--print`` is given or stdin is piped — run a single turn
headlessly and print the result."""

import argparse
import asyncio
import importlib.util
import os
import sys
from pathlib import Path

from ...thinking import THINKING_LEVELS
from ..history import PromptHistory, default_history_path


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
        help="initial permission mode (headless default: auto; interactive "
             "default: MARIM_DEFAULT_MODE). 'ask' needs the TUI",
    )
    p.add_argument(
        "--worktree", metavar="BRANCH", default=None,
        help="run inside a git worktree for BRANCH under <repo>/.worktrees/, "
             "creating it (from current HEAD) or reusing it",
    )
    p.add_argument(
        "--think",
        choices=THINKING_LEVELS,
        default=None,
        help="thinking level (reasoning effort) for this run: "
        "off/minimal/low/medium/high/xhigh. Overrides MARIM_THINKING.",
    )
    return p


def _is_headless(prompt, *, stdin_isatty: bool, textual_driver: bool = False) -> bool:
    """Headless when an explicit prompt/flag was given, or stdin is piped.

    Exception: a Textual driver (``textual serve`` / ``textual run --dev``) wires
    the app's stdio through pipes rather than a tty and signals itself via the
    ``TEXTUAL_DRIVER`` env var. ``isatty()`` is then False even though the full
    TUI is wanted (rendered through the web driver), so a set driver overrides the
    piped-stdin heuristic. An explicit prompt still forces a headless one-shot."""
    if prompt is not None:
        return True
    if textual_driver:
        return False
    return not stdin_isatty


def _tui_available() -> bool:
    """Whether the optional TUI dependency is installed. textual ships in the
    ``tui`` extra, not the core dependencies, so a bare install is headless-only."""
    return importlib.util.find_spec("textual") is not None


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

    # --think seeds MARIM_THINKING so the level flows through the normal
    # bootstrap → config → builder path (no separate wiring). A new session
    # then persists it; an existing session's saved level still wins (the
    # session override beats the env default — see Harness._resolve_thinking_id).
    if args.think is not None:
        os.environ["MARIM_THINKING"] = args.think

    # Heavy imports (pydantic_ai) deferred to here so `--help` and arg errors stay
    # fast; only an actual launch pays for the agent.
    from ...runtime.bootstrap import build_harness
    from ...runtime.permissions import Mode

    if _is_headless(
        args.prompt,
        stdin_isatty=stdin.isatty(),
        textual_driver=bool(os.environ.get("TEXTUAL_DRIVER")),
    ):
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

    if not _tui_available():
        print(
            "the interactive TUI needs the optional 'textual' dependency.\n"
            "Install the extra:  pip install 'marim-harness[tui]'\n"
            'Or run headless:    marim -p "your prompt"',
            file=err,
        )
        return 2

    # Route logs to a file before Textual takes the screen — the stderr handler
    # installed at startup still points at the real tty and would paint WARNING+
    # records straight over the live TUI (see route_logging_to_file).
    from .router import route_logging_to_file

    route_logging_to_file()

    from ..tui.app import HarnessApp

    # An explicit --mode carries into the interactive session too (it used to
    # be silently ignored on a tty); without one, the session starts in the
    # configured default (MARIM_DEFAULT_MODE, default "ask"), resolved inside
    # build_harness.
    mode = Mode(args.mode) if args.mode else None
    harness = build_harness(workspace, mode=mode, resume=args.resume)
    HarnessApp(harness, history=PromptHistory(default_history_path())).run()
    return 0
