"""The isolated git worktree a spawn runs in, as a value object.

An isolated spawn runs in its own worktree branched from HEAD so parallel
mutating spawns can't clobber each other or the main tree. ``SpawnWorktree``
owns that worktree's whole lifecycle — open/reopen, commit-and-close, and the
teardown policy a failed run follows — so the runner's foreground, background,
and CLI paths share one implementation instead of repeating the git plumbing
and the fresh-vs-resumed teardown decision in each failure arm.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass
from pathlib import Path

from ..workspace.worktree import (
    WorktreeError,
    branch_exists,
    commit_worktree,
    create_or_reuse_worktree,
    delete_branch,
    remove_worktree,
    repo_root,
)


@dataclass(frozen=True)
class SpawnWorktree:
    """A spawn's isolated worktree: its repo root, the branch it commits to, and
    the checkout path its file tools act in. Built by ``open`` (a fresh spawn) or
    ``reopen`` (a resumed spawn continuing on its prior branch)."""

    repo: Path
    branch: str
    path: Path

    @classmethod
    def open(cls, workspace_root: Path,
             branch: str) -> tuple[SpawnWorktree | None, str | None]:
        """Create an isolated worktree for a fresh spawn off the repo's HEAD.
        Returns ``(worktree, None)`` or ``(None, message)`` when the workspace
        isn't a git repo or git refuses — the message is surfaced to the
        orchestrator."""
        repo = repo_root(workspace_root)
        if repo is None:
            return None, (
                "Isolated spawn needs a git repo, but this workspace isn't one. "
                "Re-run without isolation, or initialize git first."
            )
        try:
            path = create_or_reuse_worktree(repo, branch)
        except WorktreeError as exc:
            return None, f"Couldn't create an isolated worktree: {exc}"
        return cls(repo=repo, branch=branch, path=path), None

    @classmethod
    def reopen(cls, workspace_root: Path,
               branch: str) -> tuple[SpawnWorktree | None, str | None]:
        """Reopen the worktree for a resumed spawn on its existing ``branch`` (the
        deliverable of a prior, interrupted run). Refuses — with a renderable
        message — when the branch is gone, so a resume never silently starts over
        on a fresh branch."""
        repo = repo_root(workspace_root)
        if repo is None or not branch_exists(repo, branch):
            return None, (f"Isolation branch {branch!r} no longer exists — "
                          "can't resume this isolated spawn.")
        try:
            path = create_or_reuse_worktree(repo, branch)
        except WorktreeError as exc:
            return None, f"Couldn't reopen the isolated worktree: {exc}"
        return cls(repo=repo, branch=branch, path=path), None

    def close(self) -> str:
        """Commit the spawn's changes to its branch, tear down the worktree, and
        return a note pointing the orchestrator at the branch (or noting the spawn
        changed nothing). Never raises — cleanup problems become notes."""
        try:
            summary = commit_worktree(self.path, f"sub-agent work on {self.branch}")
        except WorktreeError as exc:
            return (f"\n\n[isolated run on branch {self.branch}: commit failed ({exc}); "
                    f"worktree left at {self.path}]")
        if summary is None:
            # Nothing was produced: drop the worktree (force, since gitignored
            # leftovers may remain) and the empty branch, so spawns that change
            # nothing don't leave a trail of dead branches behind.
            self._teardown(force=True, drop_branch=True)
            return "\n\n[isolated run made no file changes]"
        self._teardown()  # keep the branch — it's the deliverable
        return (f"\n\n[isolated run committed to branch {self.branch}:\n{summary}\n"
                f"merge with `git merge {self.branch}` or review `git diff {self.branch}`]")

    def discard(self) -> None:
        """Teardown for a spawn whose fresh worktree is throwaway: force-remove the
        (possibly dirty) checkout and drop the branch, so nothing is left behind."""
        self._teardown(force=True, drop_branch=True)

    def teardown_after_failure(self, *, resumed: bool) -> None:
        """The teardown a failed or cancelled spawn follows. A FRESH spawn's
        worktree and branch are throwaway, so ``discard`` both. A RESUMED spawn's
        branch holds prior committed work the failure must not destroy, so tear
        down only the checkout and keep the branch. (``force=True`` on a resumed
        teardown is safe even when the checkout is clean — git removes it either
        way; the point is the branch survives.)"""
        if resumed:
            self._teardown(force=True)
        else:
            self.discard()

    def _teardown(self, *, force: bool = False, drop_branch: bool = False) -> None:
        """Best-effort removal of the worktree (and optionally its branch). Cleanup
        failures are swallowed — a stuck worktree is untidy, not fatal."""
        with contextlib.suppress(WorktreeError):
            remove_worktree(self.repo, self.branch, force=force)
        if drop_branch:
            with contextlib.suppress(WorktreeError):
                delete_branch(self.repo, self.branch)
