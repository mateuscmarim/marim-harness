"""Git worktree operations: create/list/remove worktrees under <repo>/.worktrees.

UI-agnostic. The only module that shells out to ``git`` for worktree management.
Every function takes an already-resolved repo root (see ``repo_root``). Git
failures surface as ``WorktreeError`` carrying git's stderr; callers decide how
to present them. Nothing here imports Deps, Textual, or the agent.
"""

from __future__ import annotations

import subprocess
from dataclasses import dataclass
from pathlib import Path

WORKTREES_DIRNAME = ".worktrees"


class WorktreeError(Exception):
    """A git worktree operation failed; message carries git's stderr."""


@dataclass(frozen=True)
class WorktreeInfo:
    path: Path        # absolute worktree path
    branch: str       # branch name without refs/heads/, or "" if detached
    head: str         # commit sha
    is_current: bool  # True if path == the `current` arg passed to list_worktrees


def _git(repo_root: Path, *args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        ["git", *args], cwd=repo_root, capture_output=True, text=True
    )


def _check(result: subprocess.CompletedProcess[str]) -> subprocess.CompletedProcess[str]:
    if result.returncode != 0:
        raise WorktreeError(result.stderr.strip() or result.stdout.strip())
    return result


def repo_root(path: Path) -> Path | None:
    """The main worktree's toplevel for the repo containing `path`, or None if
    `path` is not in a git repo, does not exist, or git is not installed.

    Uses the first row of `git worktree list --porcelain`, which git always
    reports as the main worktree — so this returns the main toplevel even when
    `path` is inside a linked worktree (e.g. under .worktrees/)."""
    try:
        result = subprocess.run(
            ["git", "worktree", "list", "--porcelain"],
            cwd=path, capture_output=True, text=True,
        )
    except (FileNotFoundError, NotADirectoryError):
        return None
    if result.returncode != 0:
        return None
    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            return Path(line[len("worktree "):])
    return None


# Characters git's check-ref-format forbids in a ref component: whitespace,
# the glob/revision metacharacters, backslash, and ASCII control chars.
_BRANCH_FORBIDDEN_CHARS = frozenset(" \t~^:?*[\\") | {chr(c) for c in range(32)} | {"\x7f"}


def _validate_branch(branch: str) -> None:
    """Reject names git itself would refuse, so a worktree branch can't shadow an
    important ref (``HEAD``, ``refs/heads/main``) or smuggle revision syntax.
    Rejects: empty, leading '-', any '.'/'..'/empty segment, the literal HEAD,
    a ``refs/`` prefix, '..'/'@{' sequences, a lone '@', a trailing '.lock' or
    '.', and the git-forbidden characters above. Slashes are otherwise allowed
    (e.g. 'feat/x')."""
    segments = branch.split("/")
    if (
        not branch
        or branch == "HEAD"
        or branch == "@"
        or branch.startswith("-")
        or branch.startswith("refs/")
        or branch.endswith(".lock")
        or branch.endswith(".")
        or ".." in branch
        or "@{" in branch
        or any(seg in ("", ".", "..") for seg in segments)
        or any(ch in _BRANCH_FORBIDDEN_CHARS for ch in branch)
    ):
        raise WorktreeError(f"invalid branch name: {branch!r}")


def branch_exists(repo_root: Path, branch: str) -> bool:
    return _git(
        repo_root, "show-ref", "--verify", "--quiet", f"refs/heads/{branch}"
    ).returncode == 0


def list_worktrees(repo_root: Path, current: Path | None = None) -> list[WorktreeInfo]:
    """Parse `git worktree list --porcelain` into WorktreeInfo rows. `is_current`
    is True for the row whose path resolves to `current`."""
    result = _check(_git(repo_root, "worktree", "list", "--porcelain"))
    cur = current.resolve() if current is not None else None
    infos: list[WorktreeInfo] = []
    path: Path | None = None
    head = ""
    branch = ""

    def flush() -> None:
        nonlocal path, head, branch
        if path is not None:
            infos.append(WorktreeInfo(
                path=path,
                branch=branch,
                head=head,
                is_current=cur is not None and path.resolve() == cur,
            ))
        path, head, branch = None, "", ""

    for line in result.stdout.splitlines():
        if line.startswith("worktree "):
            flush()
            path = Path(line[len("worktree "):])
        elif line.startswith("HEAD "):
            head = line[len("HEAD "):]
        elif line.startswith("branch "):
            branch = line[len("branch "):].removeprefix("refs/heads/")
        elif line == "detached":
            branch = ""
    flush()
    return infos


def create_or_reuse_worktree(repo_root: Path, branch: str) -> Path:
    """Return the worktree path for `branch` under <repo_root>/.worktrees/<branch>.

    - If a worktree for `branch` already exists, return its path (idempotent).
    - Else if branch `branch` exists, add a worktree checking it out there.
    - Else create `branch` from current HEAD and add the worktree.
    Raises WorktreeError on validation failure or any git failure (e.g. the
    branch is already checked out in another worktree)."""
    _validate_branch(branch)
    for info in list_worktrees(repo_root):
        if info.branch == branch:
            return info.path
    target = repo_root / WORKTREES_DIRNAME / branch
    if branch_exists(repo_root, branch):
        _check(_git(repo_root, "worktree", "add", str(target), branch))
    else:
        _check(_git(repo_root, "worktree", "add", "-b", branch, str(target)))
    return target


# Identity used for sub-agent commits, passed inline so a commit succeeds even
# when neither the worktree nor the global git config sets user.name/user.email.
_SUBAGENT_IDENTITY = (
    "-c", "user.name=marim sub-agent",
    "-c", "user.email=subagent@marim.local",
)


def commit_worktree(worktree_path: Path, message: str) -> str | None:
    """Stage and commit every change in ``worktree_path`` on its current branch.

    Returns a short diffstat summary of what was committed, or ``None`` when the
    worktree was clean (nothing to commit). The commit carries a fixed sub-agent
    identity so it lands regardless of the user's git config. Raises
    ``WorktreeError`` on any git failure."""
    _check(_git(worktree_path, "add", "-A"))
    # Nothing staged ⇒ the sub-agent changed no files; leave the branch untouched.
    if _git(worktree_path, "diff", "--cached", "--quiet").returncode == 0:
        return None
    _check(_git(worktree_path, *_SUBAGENT_IDENTITY, "commit", "-q", "-m", message))
    return _check(
        _git(worktree_path, "show", "--stat", "--format=", "HEAD")
    ).stdout.strip()


def remove_worktree(repo_root: Path, branch: str, *, force: bool = False) -> None:
    """Remove the worktree checked out on `branch`. Refuses if the worktree is
    dirty or is the current one (git's own rules) unless ``force`` is set — used
    to tear down a crashed isolated spawn's worktree. Never deletes the branch.
    Raises WorktreeError on failure.

    Resolves the worktree's real path by branch (symmetric with
    ``create_or_reuse_worktree``, which reuses any existing worktree for the
    branch) rather than assuming the canonical ``.worktrees/<branch>`` location —
    a reused worktree may live elsewhere, and a fixed path would error."""
    _validate_branch(branch)
    target = repo_root / WORKTREES_DIRNAME / branch
    for info in list_worktrees(repo_root):
        if info.branch == branch:
            target = info.path
            break
    args = ["worktree", "remove"]
    if force:
        args.append("--force")
    args.append(str(target))
    _check(_git(repo_root, *args))


def delete_branch(repo_root: Path, branch: str) -> None:
    """Force-delete `branch` (``git branch -D``). Used to drop an isolation branch
    that never advanced past HEAD or whose work was abandoned, so they don't pile
    up. Raises WorktreeError if the branch is missing or still checked out (remove
    its worktree first)."""
    _validate_branch(branch)
    _check(_git(repo_root, "branch", "-D", branch))
