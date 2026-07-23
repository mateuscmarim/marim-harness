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

# Committer identity passed inline to commit-tree so a snapshot commit succeeds
# even when neither the repo nor the global git config sets user.name/user.email
# (fresh machines, CI). Without it commit-tree raises CalledProcessError,
# capture() returns None, and checkpoints + file-rewind go silently dead for the
# whole session. worktree.py solves the same problem the same way (its
# ``_SUBAGENT_IDENTITY``); this is the snapshot-side twin.
_COMMITTER_IDENTITY = (
    "-c", "user.name=marim checkpoint",
    "-c", "user.email=checkpoint@marim.local",
)


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

    def _run(
        self, *args: str, env: dict[str, str] | None = None, input: str | None = None
    ) -> str:
        """Run a git command with cwd=workspace_root (the actual working tree).

        For linked worktrees this is the linked worktree directory, NOT the
        main-worktree toplevel returned by repo_root().  Refs (refs/marim/*)
        are shared across all worktrees, so update-ref/read-tree/etc. still
        resolve correctly from here."""
        return subprocess.run(
            ["git", *args], cwd=self.workspace_root, env=env, input=input,
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
                reused = self._reuse_last_commit(ref)
                if reused is not None:
                    return reused
                # Fast path failed (the cached commit was pruned/corrupt, so
                # update-ref errored). The fingerprint is now stale; it has been
                # cleared, so fall through to the full capture below instead of
                # letting the outer handler return None and go dead for the turn.
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                # Stage the whole working tree (tracked + untracked, honoring
                # .gitignore) into the throwaway index, then snapshot it.
                self._run("add", "-A", env=env)
                # ``add -A`` against the FRESH index applies ignore rules to
                # every file (the fresh index doesn't know what's tracked), so a
                # file that is gitignored yet tracked in the real index (a
                # force-added .env) gets silently skipped. It must be in the
                # snapshot: _present_files lists it from the real index, so if
                # the tree omits it, restore's set-difference marks it "extra"
                # and deletes it — permanent data loss. Stage those explicitly.
                self._stage_tracked_ignored(env)
                tree = self._run("write-tree", env=env)
                # Pass the committer identity inline (see _COMMITTER_IDENTITY) so
                # this never depends on the user's git config being set.
                commit = self._run(
                    *_COMMITTER_IDENTITY, "commit-tree", tree, "-m", message, env=env
                )
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

    def _reuse_last_commit(self, ref: str) -> str | None:
        """Re-point ``ref`` at the cached fast-path commit, returning it on
        success. Returns None — after clearing the now-untrustworthy fingerprint —
        when update-ref fails, which happens if that cached commit was pruned or
        the object store was rewritten out from under us (``git gc``, a manual
        prune, a repack). Without the clear, every subsequent clean capture at
        this HEAD would keep matching the same dead fingerprint and keep returning
        None, silently disabling checkpoints for the rest of the session; clearing
        it lets the caller fall through to a full, self-healing capture."""
        commit = self._last_commit
        if commit is None:  # caller already guards this; narrows for the type checker
            return None
        try:
            self._run("update-ref", ref, commit)
            return commit
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint fast-path reuse failed, forcing full capture: %s",
                         exc.stderr or exc)
            self._last_clean_head = None
            self._last_commit = None
            return None

    def _stage_tracked_ignored(self, env: dict[str, str]) -> None:
        """Stage tracked-but-gitignored files into the throwaway index (see the
        call site in capture() for why ``add -A`` misses them).

        ``ls-files -c -i --exclude-standard`` reads the REAL index and lists
        exactly the tracked files matched by ignore rules — the same "tracked"
        notion _present_files uses, which is what keeps capture and the restore
        deletion set consistent. Untracked-ignored files are NOT listed, so they
        stay out of the snapshot (and _present_files excludes them too, so they
        survive restore untouched — both invariants hold). Staging goes through
        ``update-index --add --stdin -z`` (plumbing: no ignore rules, paths are
        literal — no pathspec globbing, NUL-safe for newline filenames). A
        tracked-ignored file deleted from disk is filtered out (update-index
        --add errors on missing files); it is then simply absent from the
        snapshot, matching add -A's treatment of deleted tracked files."""
        out = self._run("ls-files", "-z", "-c", "-i", "--exclude-standard")
        if not out:
            return
        # lexists, not exists: a tracked broken symlink is still on disk and
        # stageable; exists() would follow the link and wrongly drop it.
        present = sorted(
            p for p in self._split_nul(out) if os.path.lexists(self.workspace_root / p)
        )
        if not present:
            return
        self._run(
            "update-index", "--add", "-z", "--stdin",
            env=env, input="\0".join(present) + "\0",
        )

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
        # git reports an untracked directory that is itself a git repo — a
        # submodule, or one of marim's own ``.worktrees/<branch>`` spawn worktrees
        # (nothing gitignores them) — as a single entry with a TRAILING SLASH
        # (``.worktrees/feat/``), while ``add -A`` stages the same path as a gitlink
        # in the snapshot tree WITHOUT the slash (``.worktrees/feat``). A POSIX path
        # can never legitimately end in ``/``, so stripping it is always safe and
        # makes the two representations compare equal — otherwise the set difference
        # in _remove_extra_files marks the nested repo for deletion, unlink() of a
        # directory raises, and restore() reports failure on *every* rewind forever.
        return {f.rstrip("/") for f in files}

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
            path = self.workspace_root / rel
            # is_dir() FOLLOWS symlinks, so an extra symlink-to-directory would be
            # misclassified as a nested repo, skipped, and restore would report
            # success while silently leaving it behind. A symlink is a file-sized
            # entry (git lists it as one); unlink() removes just the link, never
            # its target — so it takes the normal file-removal path below.
            if path.is_dir() and not path.is_symlink():
                # A directory here is a nested git repo/worktree gitlink (see
                # _present_files): it is outside what the snapshot captured (a
                # gitlink, not tracked content), and recursively deleting a spawn
                # worktree on rewind would be destructive. Never our file to
                # remove — leave it in place. (Belt-and-suspenders with the
                # trailing-slash normalization: this also covers a nested repo
                # that post-dates the checkpoint and so is absent from the tree.)
                continue
            try:
                path.unlink()
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
            # 1. Restore tracked + untracked captured content via a throwaway index,
            #    so the user's real index/HEAD are untouched. Done FIRST — before
            #    computing which present files are "extra" — for two reasons:
            #    (a) it reinstates the capture-time .gitignore on disk, so the
            #        _present_files() listing below re-honors it. Without this, a
            #        file that was git-ignored at capture (never in the snapshot)
            #        but un-ignored at restore because the agent deleted .gitignore
            #        would show up as "extra" and be silently deleted (.env, local
            #        DBs) — real data loss. With the captured .gitignore back,
            #        --exclude-standard filters such files out and they survive.
            #    (b) a bad/absent commit fails here before any deletion runs, so a
            #        failed restore never leaves files removed.
            with _temp_index() as idx:
                env = {**os.environ, "GIT_INDEX_FILE": idx}
                self._run("read-tree", commit, env=env)
                self._run("checkout-index", "-a", "-f", env=env)
            # 2. Remove files that exist now but not in the target snapshot.
            failed = self._remove_extra_files(self._tree_files(commit))
            if failed:
                logger.debug(
                    "checkpoint restore left %d file(s) behind: %s", len(failed), failed
                )
                return False
            return True
        except subprocess.CalledProcessError as exc:
            logger.debug("checkpoint restore failed: %s", exc.stderr or exc)
            return False


def delete_checkpoint_refs(workspace_root, session_id: str) -> None:
    """Best-effort: delete every ``refs/marim/checkpoints/<session_id>/*`` ref.

    Used by session deletion. A session's checkpoint refs pin whole-working-tree
    snapshot commits — including untracked files, potentially secrets — in
    ``.git`` forever; without this, deleting the session leaks all of them.
    Silently a no-op when the workspace isn't a git repo or git is missing —
    session deletion must not fail over ref hygiene."""
    prefix = f"refs/marim/checkpoints/{session_id}"
    try:
        out = subprocess.run(
            ["git", "for-each-ref", "--format=%(refname)", prefix],
            cwd=workspace_root, capture_output=True, text=True, check=True,
        ).stdout
    except (OSError, subprocess.CalledProcessError):
        return
    for ref in out.splitlines():
        ref = ref.strip()
        # Same guard as GitSnapshotter.delete: never delete outside our
        # namespace, even if for-each-ref returns something unexpected.
        if not ref.startswith("refs/marim/"):
            continue
        subprocess.run(
            ["git", "update-ref", "-d", ref],
            cwd=workspace_root, capture_output=True, text=True,
        )
