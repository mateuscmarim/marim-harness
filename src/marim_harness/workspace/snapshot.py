# src/marim_harness/workspace/snapshot.py
"""Shadow git snapshots for checkpoints. Captures the working tree into a
commit under a private ``refs/marim/checkpoints/*`` ref — without touching the
user's branch, index, or HEAD — and restores the working tree from one.

This is the file-state half of a checkpoint; the conversation half lives in
``session/checkpoints.py``. Like ``worktree.py``, it is the only place (besides
that module) that shells out to git, and it never mutates user-visible git
state except working-tree files on restore."""

from __future__ import annotations

import contextlib
import logging
import os
import subprocess
import tempfile
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

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
        with contextlib.suppress(OSError):
            os.unlink(name)


class GitSnapshotter:
    def __init__(self, workspace_root: Path) -> None:
        self.workspace_root = Path(workspace_root)
        # repo_root() shells out to `git worktree list`, but the repo root is
        # immutable for the workspace's lifetime — so resolve it lazily once and
        # cache it. None is a legitimate result here (workspace is not a git repo),
        # so a separate flag, not None, marks "not yet resolved"; the not-a-repo
        # answer is cached too, keeping capture/delete/restore's no-op fast.
        self._repo_resolved = False
        self._repo_root: Path | None = None
        # Reuse fingerprint for the chat-only fast path. `_last_clean_head` is the
        # HEAD sha of the most recent capture that found a *clean* working tree, and
        # `_last_commit` is the snapshot commit that capture produced. A later clean
        # capture at the same HEAD has byte-identical content (a clean tree equals
        # HEAD's tree, and a commit sha pins its tree), so it can re-point its ref at
        # `_last_commit` instead of restaging the whole tree with `git add -A`. A
        # dirty capture clears the fingerprint, since porcelain status alone can't
        # tell two different dirty contents apart.
        self._last_clean_head: str | None = None
        self._last_commit: str | None = None

    def _repo(self) -> Path | None:
        """Return the main-worktree root (used only as an is-a-repo guard),
        or None when workspace_root is not inside a git repository. Memoized:
        the answer (including None) never changes for this workspace."""
        if not self._repo_resolved:
            self._repo_root = repo_root(self.workspace_root)
            self._repo_resolved = True
        return self._repo_root

    def _clean_head(self) -> str | None:
        """Return HEAD's sha when the working tree is clean, else None.

        `git status --porcelain` is cheap (it rides the real index's stat cache
        and honors .gitignore); tracked changes and untracked non-ignored files
        both show up. With one exception (handled below) an empty status means the
        working tree content equals HEAD's tree, so the content is pinned by the
        HEAD sha — which is what makes reusing a prior snapshot at the same HEAD
        safe. A dirty tree returns None (content not pinned by HEAD alone), forcing
        a full capture; an unborn branch / git hiccup also returns None (same).

        The exception: a file marked `skip-worktree` or `assume-unchanged` is
        suppressed from `status` even when its on-disk content differs from HEAD,
        so a clean status would NOT imply the tree equals HEAD's tree. The full
        path's fresh, throwaway index carries neither bit, so its `add -A` captures
        such a file's live content — but the fast path would reuse a commit built
        from the file's earlier content and silently restore stale data on rewind.
        So if any such bit is set we return None and force the full capture."""
        try:
            if self._run("status", "--porcelain"):
                return None
            if self._index_hides_changes():
                return None
            return self._run("rev-parse", "HEAD")
        except subprocess.CalledProcessError:
            return None

    def _index_hides_changes(self) -> bool:
        """True if any tracked file carries the skip-worktree or assume-unchanged
        bit (which hide on-disk changes from `git status`). `git ls-files -v` tags
        each entry: a lowercase tag means assume-unchanged, `S` means skip-worktree.
        Reading the index is far cheaper than the `add -A` the fast path avoids."""
        for line in self._run("ls-files", "-v").splitlines():
            if line and (line[0].islower() or line[0] == "S"):
                return True
        return False

    def _run(self, *args: str, env: dict[str, str] | None = None) -> str:
        """Run a git command with cwd=workspace_root (the actual working tree).

        For linked worktrees this is the linked worktree directory, NOT the
        main-worktree toplevel returned by repo_root().  Refs (refs/marim/*)
        are shared across all worktrees, so update-ref/read-tree/etc. still
        resolve correctly from here."""
        return subprocess.run(
            ["git", *args], cwd=self.workspace_root, env=env,
            capture_output=True, text=True, check=True,
        ).stdout.strip()

    def capture(self, ref: str, message: str) -> str | None:
        if not ref.startswith("refs/marim/"):
            raise ValueError(f"refusing to write ref outside refs/marim/: {ref!r}")
        if self._repo() is None:
            return None
        try:
            clean_head = self._clean_head()
            # Fast path: the working tree is clean at the same HEAD as our last
            # capture, so its content is byte-identical to the snapshot we already
            # made. Re-point this checkpoint's ref at that commit instead of
            # restaging the entire working tree (`git add -A`, whose cost scales
            # with repo size) — this is what spares chat-only turns the snapshot
            # cost. The fresh ref keeps the shared commit reachable (so GC won't
            # drop it and restore() still resolves it); nothing keys on snapshots
            # having distinct shas, so two checkpoints sharing a commit is fine.
            if (
                clean_head is not None
                and clean_head == self._last_clean_head
                and self._last_commit is not None
            ):
                self._run("update-ref", ref, self._last_commit)
                return self._last_commit
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                # Stage the whole working tree (tracked + untracked, honoring
                # .gitignore) into the throwaway index, then snapshot it.
                self._run("add", "-A", env=env)
                tree = self._run("write-tree", env=env)
                commit = self._run("commit-tree", tree, "-m", message, env=env)
            # Keep the commit reachable so GC won't drop it.
            self._run("update-ref", ref, commit)
            # Record the content fingerprint so the next clean turn at this HEAD
            # reuses this commit. A dirty capture (clean_head is None) clears it,
            # since its content isn't pinned by HEAD alone.
            self._last_clean_head = clean_head
            self._last_commit = commit
            return commit
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint capture failed: %s", exc.stderr or exc)
            return None

    @staticmethod
    def _split_nul(out: str) -> set[str]:
        """Split NUL-delimited git output into a set of paths. Using ``-z`` and
        splitting on ``\\0`` (not ``.splitlines()``) keeps a filename containing
        a newline from being shattered into bogus entries — critical here because
        this set drives a *delete* path, where a misparse could unlink the wrong
        file. The trailing NUL leaves an empty final field, which we drop."""
        return {p for p in out.split("\0") if p}

    def _tree_files(self, commit: str) -> set[str]:
        # -z: NUL-delimited, newline-safe filenames (see _split_nul).
        out = self._run("ls-tree", "-r", "-z", "--name-only", commit)
        return self._split_nul(out) if out else set()

    def _present_files(self) -> set[str]:
        # -z on both listings so filenames with newlines survive the parse.
        tracked = self._run("ls-files", "-z")
        untracked = self._run("ls-files", "-z", "--others", "--exclude-standard")
        files: set[str] = set()
        for blob in (tracked, untracked):
            if blob:
                files.update(self._split_nul(blob))
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

    def _remove_extra_files(self, target: set[str]) -> list[str]:
        """Delete files present now but absent in ``target`` (created after the
        checkpoint). Returns paths that could not be removed. Scoped to the diff
        — never a blanket clean; git-ignored files are excluded by
        _present_files and intentionally left untouched."""
        failed: list[str] = []
        for rel in self._present_files() - target:
            try:
                (self.workspace_root / rel).unlink()
            except FileNotFoundError:
                pass  # already gone — that's the goal, not a failure
            except OSError as exc:
                # Couldn't delete a file that should be absent after restore.
                # Don't swallow it: a partial restore left a stale file behind,
                # so we must not report success (the bug behind the old blanket
                # suppress(OSError) + unconditional True).
                failed.append(rel)
                logger.debug("checkpoint restore could not remove %s: %s", rel, exc)
        return failed

    def restore(self, commit: str) -> bool:
        """Restore the working tree to ``commit``. Returns True on success, False
        when there is no repo or git fails — so the caller never reports a partial
        or failed rewind as if it succeeded. Capturing a pre-restore safety
        snapshot is the caller's job (CheckpointManager), which owns the
        session-namespaced ref and the undo path."""
        if self._repo() is None:
            return False
        try:
            # 1. Remove files that exist now but not in the target snapshot.
            failed = self._remove_extra_files(self._tree_files(commit))
            # 2. Restore tracked + untracked content via a throwaway index, so
            #    the user's real index/HEAD are untouched.
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                self._run("read-tree", commit, env=env)
                self._run("checkout-index", "-a", "-f", env=env)
            if failed:
                logger.debug(
                    "checkpoint restore left %d file(s) behind: %s", len(failed), failed
                )
                return False
            return True
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint restore failed: %s", exc.stderr or exc)
            return False
