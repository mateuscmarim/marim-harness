"""The default invocation (no management subcommand): launch the interactive
TUI, or — when ``-p/--print`` is given or stdin is piped — run a single turn
headlessly and print the result."""

import argparse
import asyncio
import importlib.util
import os
import sys
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, TypeAlias

from ...thinking import THINKING_LEVELS
from ..history import PromptHistory, default_history_path

if TYPE_CHECKING:
    from ...runtime.harness import Harness
    from ...server.attach import RemoteTarget
    from ...session.claim import SessionClaim


def _version() -> str:
    """The installed package version, or a placeholder when running from a source
    tree that was never installed (no dist metadata)."""
    from ..branding import package_version

    return package_version()


def _build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="marim")
    p.add_argument(
        "--version",
        action="version",
        version=f"%(prog)s {_version()}",
    )
    p.add_argument(
        "workspace",
        nargs="?",
        default=None,
        help="workspace directory (defaults to the current directory)",
    )
    p.add_argument(
        "--resume",
        action="store_true",
        help="resume the saved conversation for this workspace",
    )
    p.add_argument(
        "--session",
        default=None,
        metavar="SESSION_ID",
        help=(
            "open a specific saved session by id; when a running `marim serve` "
            "daemon owns it, the TUI attaches to the daemon instead of taking over"
        ),
    )
    p.add_argument(
        "-p",
        "--print",
        nargs="?",
        const=True,
        default=None,
        dest="prompt",
        metavar="PROMPT",
        help="run headlessly: PROMPT, or read the prompt from stdin if omitted",
    )
    p.add_argument(
        "--output-format",
        choices=["text", "json", "stream-json"],
        default="text",
        dest="output_format",
        help="headless output format (default: text)",
    )
    p.add_argument(
        "--mode",
        choices=["plan", "auto"],
        default=None,
        help="initial permission mode (headless default: auto; interactive "
        "default: MARIM_DEFAULT_MODE). 'ask' needs the TUI",
    )
    p.add_argument(
        "--worktree",
        metavar="BRANCH",
        default=None,
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


def _acquire_session(harness, *, kind: str, err) -> "tuple[SessionClaim | None, bool]":
    """Take ownership of this run's session, or explain who already has it.

    Returns ``(claim, may_proceed)``. A ``None`` claim with ``may_proceed`` True
    means there was nothing to claim: an anonymous session has no file on disk,
    so no other process can be overwriting it.
    """
    from ...session.claim import read_holder, try_acquire

    session = getattr(harness, "session", None)
    if session is None:
        return None, True
    store = getattr(session, "store", None)
    if store is None:
        return None, True
    claim = try_acquire(store.path, kind=kind)
    if claim is not None:
        return claim, True
    holder = read_holder(store.path)
    who = holder.describe() if holder is not None else "another process"
    print(
        f"session {store.session_id} is already open in {who}.\n"
        "Close it there first, or start a new session (drop --resume).",
        file=err,
    )
    return None, False


def _resolve_target_session(workspace: Path, resume: bool) -> str | None:
    """The session id a launch will reattach to, resolved BEFORE build_harness
    so ownership can be taken before any store read. --resume is a flag: it
    means the workspace's most recent session (bootstrap.py's manager.latest()
    rule). No resume (or no sessions yet) -> None: a fresh session's id only
    exists after the build, so it is claimed post-build by _acquire_session."""
    if not resume:
        return None
    from ...session.store import SessionManager

    latest = SessionManager(workspace).latest()
    return latest.id if latest is not None else None


def _claim_target(workspace: Path, target: str | None, *, kind: str, err):
    """Claim a pre-resolved target session before build_harness reads it.

    Returns ``(claim, may_proceed)``; ``may_proceed`` False means the refusal
    message was printed. ``target`` None -> ``(None, True)``: nothing to claim
    up front. A target whose file vanished between being resolved (latest())
    and being claimed here is also refused: the claim happily creates a fresh
    sidecar for a missing id, and build_harness would then load that id as an
    empty session — resurrecting a deleted one on the next persist — rather
    than reporting that it's gone."""
    if target is None:
        return None, True
    if not _is_session_id(target):
        # ``--session`` is user text that becomes a file name under the
        # sessions dir; a path-like value ("../other-workspace/<id>") would
        # claim — and then load — a session that is not this workspace's.
        print(f"session {target!r} is not a session id (ids are bare names).", file=err)
        return None, False
    from ...session.claim import try_acquire
    from ...session.store import SessionManager

    session_path = SessionManager(workspace).session_path(target)
    claim = try_acquire(session_path, kind=kind)
    if claim is not None:
        if not session_path.exists():
            claim.release()
            print(
                f"session {target} no longer exists (nothing saved at {session_path}).",
                file=err,
            )
            return None, False
        notice = _stale_daemon_notice(claim, target)
        if notice is not None:
            # Headless reads this on stderr. The TUI prints it too, but
            # Textual paints over it — _start_tui hands the same text to the
            # app, which shows it in the transcript once it is up.
            print(notice, file=err)
        return claim, True
    return _refuse_or_attach(workspace, target, session_path, kind=kind, err=err)


def _stale_daemon_notice(claim: "SessionClaim | None", target: str | None) -> str | None:
    """The line to show when a launch that could have attached took the
    session over instead: the claim file said a ``marim serve`` daemon held
    ``target``, but its lock was free (the daemon died or let the session
    go), so this process now drives it locally. Silent for any other
    predecessor — a TUI or headless run that exited is the normal case, not
    news. Without it the fallback is invisible: the only tell was that the
    status bar did NOT read ``daemon``."""
    if claim is None or target is None:
        return None
    previous = claim.displaced
    if previous is None or previous.kind != "daemon":
        return None
    where = f" at {previous.endpoint}" if previous.endpoint else ""
    return (
        f"session {target} was held by the marim serve daemon{where} (pid {previous.pid}), "
        "but that claim is stale — the daemon no longer holds it. Running the session "
        "locally in this process, not attached; restart the daemon (and warm the session "
        "there) to attach instead."
    )


def _is_session_id(target: str) -> bool:
    """A bare file-name component: no separators (either flavor), not a
    dot-name. ``--resume`` and the picker only ever hand over ids the manager
    listed, so this guards the one path that takes the id from the command
    line."""
    return (
        target not in ("", ".", "..")
        and "/" not in target
        and "\\" not in target
        and Path(target).name == target
    )


def _refuse_or_attach(workspace: Path, target: str, session_path: Path, *, kind: str, err):
    """The claim on ``target`` is held elsewhere. An interactive launch can
    still open the session when the holder is a reachable ``marim serve``
    daemon: the TUI then attaches over the daemon's API (phase 4a) instead of
    taking the session over. Returns ``(RemoteTarget, True)`` in that case —
    the caller launches the remote TUI and never builds a Harness. Headless
    never attaches: ``marim -p`` runs the turn in its own process or not at
    all. Any other holder (a TUI, a daemon that failed a probe) is the usual
    refusal, with the probe's reason printed first so the user knows why the
    attach was skipped."""
    from ...server.attach import discover
    from ...session.claim import read_holder
    from .serve import _default_state_dir

    if kind == "tui":
        decision = discover(workspace, target, session_path, state_dir=_default_state_dir())
        if decision.target is not None:
            return decision.target, True
        if decision.reason is not None:
            print(f"not attaching: {decision.reason}.", file=err)
    holder = read_holder(session_path)
    who = holder.describe() if holder is not None else "another process"
    print(
        f"session {target} is already open in {who}.\n"
        "Close it there first, or start a new session (drop --resume/--session).",
        file=err,
    )
    return None, False


def _run_claimed(
    harness, *, kind: str, err, run: Callable[[], int], claim: "SessionClaim | None" = None
) -> int:
    """Run `run()` under a session claim, adopted by the Harness, and release
    it on the way out.

    `claim` is a PRE-BUILD claim for a resumed session (ownership taken before
    build_harness read the store — see _claim_target). When None, the session
    is claimed post-build (_acquire_session: fresh sessions have their id only
    after the build). Either way the claim is adopted by the Harness so an
    in-run session switch can move it, and released through the Harness on
    exit — idempotent, since a switch may have already swapped it.

    Returns 2 without calling `run` when a post-build claim finds the session
    owned elsewhere.
    """
    if claim is None:
        claim, may_proceed = _acquire_session(harness, kind=kind, err=err)
        if not may_proceed:
            return 2
    harness.adopt_claim(claim, kind=kind)
    try:
        return run()
    finally:
        harness.release_claim()


def _launch_tui(harness, *, notice: str | None = None) -> int:
    """Run the TUI on a process-local Harness. ``notice`` is a launch-time
    line for the transcript (a stale-daemon-claim fallback, see
    _stale_daemon_notice): anything printed to stderr before Textual starts
    is painted over, so it has to travel into the app."""
    from ..tui.app import HarnessApp

    notices = [notice] if notice is not None else []
    HarnessApp(harness, history=PromptHistory(default_history_path()), notices=notices).run()
    return 0


def _launch_remote_tui(target: "RemoteTarget") -> int:
    """Run the TUI attached to a daemon-owned session: no Harness, no claim —
    the daemon keeps both, and the app talks to it over REST + WebSocket."""
    from ..tui.app import HarnessApp

    HarnessApp(None, history=PromptHistory(default_history_path()), remote=target).run()
    return 0


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


def _launch_target(args, workspace: Path) -> str | None:
    """The session id this launch opens: an explicit ``--session`` wins,
    ``--resume`` means the workspace's latest, neither means a fresh one."""
    if args.session is not None:
        return str(args.session)  # "" included: refused by _claim_target, not a fresh session
    return _resolve_target_session(workspace, args.resume)


_Built: TypeAlias = "tuple[Harness, SessionClaim | None] | tuple[None, RemoteTarget] | None"


def _claim_and_build(workspace: Path, *, target: str | None, mode, kind: str, err) -> _Built:
    """Claim the target session, then build the Harness onto it.

    Returns ``(harness, claim) | None`` — ``None`` means the refusal was
    already printed and the caller should return 2. A build failure releases
    the pre-build claim (nothing else will) before re-raising. When the claim
    step decided to *attach* instead (a daemon owns the target and this is an
    interactive launch), the result is ``(None, RemoteTarget)``: no Harness is
    built in this process at all. Callers tell the two shapes apart on the
    first slot (``built[0] is None``), which is how pyright narrows a tuple
    union — an unpacked ``harness is None`` would not narrow ``claim``.
    """
    from ...runtime.bootstrap import build_harness
    from ...server.attach import RemoteTarget

    claim, may_proceed = _claim_target(workspace, target, kind=kind, err=err)
    if not may_proceed:
        return None
    if isinstance(claim, RemoteTarget):
        return None, claim
    try:
        harness = build_harness(
            workspace,
            mode=mode,
            session_id=target,
            # Never resume=True: build_harness's resume flag performs its OWN
            # unclaimed latest() lookup (bootstrap.py:133) — the exact read
            # this function exists to claim BEFORE. If resolve found no
            # target, the honest reading of "--resume with no sessions" is a
            # fresh session (one created in the meantime would otherwise be
            # picked up unclaimed); if it did, session_id above pins it.
            resume=False,
        )
    except BaseException:
        if claim is not None:
            claim.release()
        raise
    return harness, claim


def _start_headless(args, workspace: Path, stdin, out, err) -> int:
    """The headless launch branch of :func:`run_default`: read the prompt, claim
    + build, and run the turn under the claim. Returns the exit code."""
    prompt = args.prompt if isinstance(args.prompt, str) else stdin.read()
    prompt = (prompt or "").strip()
    if not prompt:
        print("no prompt provided", file=err)
        return 2
    from ...runtime.permissions import Mode
    from .headless import run_headless

    mode = Mode(args.mode) if args.mode else Mode.auto
    target = _launch_target(args, workspace)
    built = _claim_and_build(workspace, target=target, mode=mode, kind="headless", err=err)
    if built is None:
        return 2
    # Only an interactive launch attaches (_refuse_or_attach): a headless
    # build always carries a Harness.
    assert built[0] is not None
    harness, claim = built
    return _run_claimed(
        harness,
        kind="headless",
        err=err,
        claim=claim,
        run=lambda: asyncio.run(
            run_headless(harness, prompt, args.output_format, out=out, err=err)
        ),
    )


def _start_tui(args, workspace: Path, err) -> int:
    """The interactive branch of :func:`run_default`: route logging away from
    the tty, claim + build, and hand the screen to Textual under the claim."""
    # Route logs to a file before Textual takes the screen — the stderr handler
    # installed at startup still points at the real tty and would paint WARNING+
    # records straight over the live TUI (see route_logging_to_file).
    from ...runtime.permissions import Mode
    from .router import route_logging_to_file

    route_logging_to_file()

    # An explicit --mode carries into the interactive session too (it used to
    # be silently ignored on a tty); without one, the session starts in the
    # configured default (MARIM_DEFAULT_MODE, default "ask"), resolved inside
    # build_harness.
    mode = Mode(args.mode) if args.mode else None
    target = _launch_target(args, workspace)
    built = _claim_and_build(workspace, target=target, mode=mode, kind="tui", err=err)
    if built is None:
        return 2
    if built[0] is None:
        # The daemon owns the session: attach instead of taking it over. The
        # claim stays with the daemon, so there is nothing to adopt or release.
        return _launch_remote_tui(built[1])
    harness, claim = built
    notice = _stale_daemon_notice(claim, target)
    return _run_claimed(
        harness, kind="tui", err=err, claim=claim, run=lambda: _launch_tui(harness, notice=notice)
    )


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

    # Heavy imports (pydantic_ai) are deferred inside the two launch helpers
    # below, so `--help` and arg errors stay fast; only an actual launch pays
    # for the agent.
    if _is_headless(
        args.prompt,
        stdin_isatty=stdin.isatty(),
        textual_driver=bool(os.environ.get("TEXTUAL_DRIVER")),
    ):
        return _start_headless(args, workspace, stdin, out, err)

    if not _tui_available():
        print(
            "the interactive TUI needs the optional 'textual' dependency.\n"
            "Install the extra:  pip install 'marim-harness[tui]'\n"
            'Or run headless:    marim -p "your prompt"',
            file=err,
        )
        return 2

    return _start_tui(args, workspace, err)
