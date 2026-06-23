# src/marim_harness/workspace/snapshot.py
"""Shadow git snapshots for checkpoints. Captures the working tree into a
commit under a private ``refs/marim/checkpoints/*`` ref — without touching the
user's branch, index, or HEAD — and restores the working tree from one.

This is the file-state half of a checkpoint; the conversation half lives in
``session/checkpoints.py``. Like ``worktree.py``, it is the only place (besides
that module) that shells out to git, and it never mutates user-visible git
state except working-tree files on restore."""

from __future__ import annotations

import logging
import os
import subprocess
import tempfile
from contextlib import contextmanager
from pathlib import Path
from typing import Iterator, Optional

from .worktree import repo_root

logger = logging.getLogger(__name__)


@contextmanager
def _temp_index() -> Iterator[str]:
    """A throwaway git index file, so staging never touches the user's index."""
    fd, name = tempfile.mkstemp(suffix=".marim-index")
    os.close(fd)
    os.unlink(name)  # git wants to create it itself; we only need a unique path
    try:
        yield name
    finally:
        try:
            os.unlink(name)
        except OSError:
            pass


class GitSnapshotter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)

    def _repo(self) -> Optional[Path]:
        """Return the main-worktree root (used only as an is-a-repo guard),
        or None when workspace_root is not inside a git repository."""
        return repo_root(self.workspace_root)

    def _run(self, *args: str, env: Optional[dict[str, str]] = None) -> str:
        """Run a git command with cwd=workspace_root (the actual working tree).

        For linked worktrees this is the linked worktree directory, NOT the
        main-worktree toplevel returned by repo_root().  Refs (refs/marim/*)
        are shared across all worktrees, so update-ref/read-tree/etc. still
        resolve correctly from here."""
        return subprocess.run(
            ["git", *args], cwd=self.workspace_root, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def capture(self, ref: str, message: str) -> Optional[str]:
        if not ref.startswith("refs/marim/"):
            raise ValueError(f"refusing to write ref outside refs/marim/: {ref!r}")
        if self._repo() is None:
            return None
        try:
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                # Stage the whole working tree (tracked + untracked, honoring
                # .gitignore) into the throwaway index, then snapshot it.
                self._run("add", "-A", env=env)
                tree = self._run("write-tree", env=env)
                commit = self._run("commit-tree", tree, "-m", message, env=env)
            # Keep the commit reachable so GC won't drop it.
            self._run("update-ref", ref, commit)
            return commit
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint capture failed: %s", exc.stderr or exc)
            return None

    def _tree_files(self, commit: str) -> set[str]:
        out = self._run("ls-tree", "-r", "--name-only", commit)
        return set(out.splitlines()) if out else set()

    def _present_files(self) -> set[str]:
        tracked = self._run("ls-files")
        untracked = self._run("ls-files", "--others", "--exclude-standard")
        files = set()
        for blob in (tracked, untracked):
            if blob:
                files.update(blob.splitlines())
        return files

    def delete(self, ref: str) -> None:
        if not ref.startswith("refs/marim/"):
            raise ValueError(f"refusing to delete ref outside refs/marim/: {ref!r}")
        if self._repo() is None:
            return
        # Best-effort: deleting an already-absent ref is fine.
        subprocess.run(
            ["git", "update-ref", "-d", ref], cwd=self.workspace_root,
            capture_output=True, text=True,
        )

    def restore(self, commit: str) -> None:
        if self._repo() is None:
            return
        try:
            # 1. Safety net: snapshot the current state so the rewind is undoable.
            pre = self.capture("refs/marim/checkpoints/_pre_restore", "pre-restore safety snapshot")
            if pre is None:
                logger.warning(
                    "restore: pre-restore safety snapshot failed; "
                    "proceeding without a recovery point"
                )
            # 2. Remove files that exist now but not in the target snapshot
            #    (created after the checkpoint). Scoped to the diff — never a
            #    blanket clean.
            target = self._tree_files(commit)
            # Remove files created after the checkpoint (present now, absent in the
            # target tree). Scoped to the diff — never a blanket clean. Git-ignored
            # files are excluded by _present_files and intentionally left untouched.
            for rel in self._present_files() - target:
                try:
                    (self.workspace_root / rel).unlink()
                except OSError:
                    pass
            # 3. Restore tracked + untracked content via a throwaway index, so
            #    the user's real index/HEAD are untouched.
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                self._run("read-tree", commit, env=env)
                self._run("checkout-index", "-a", "-f", env=env)
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint restore failed: %s", exc.stderr or exc)
