"""Per-session checkpoints: a capture of conversation length + an optional
shadow git commit of the working tree, taken at the start of each turn so a
session can be rewound to an earlier point.

The git work is injected as a ``Snapshotter`` so this module stays
git-agnostic and unit-testable; the real implementation lives in
``workspace/snapshot.py``."""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Protocol

from ..atomic_io import atomic_write_text, file_lock

if TYPE_CHECKING:
    from .ctrl import SessionController
    from .store import SessionStore


@dataclass
class Checkpoint:
    index: int            # monotonic ordinal, unique within a session
    history_len: int      # len(history) captured before this turn ran
    commit: str | None # shadow commit sha (restore target), or None
    created: str          # ISO-8601 UTC timestamp
    prompt_preview: str   # first ~80 chars of the turn's user prompt

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "history_len": self.history_len,
            "commit": self.commit,
            "created": self.created,
            "prompt_preview": self.prompt_preview,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Checkpoint:
        return cls(
            index=int(d["index"]),
            history_len=int(d["history_len"]),
            commit=d.get("commit"),
            created=str(d.get("created", "")),
            prompt_preview=str(d.get("prompt_preview", "")),
        )


class Snapshotter(Protocol):
    """Captures/restores the working tree behind a checkpoint. The Null
    implementation makes conversation-only rewind work with no git."""

    def capture(self, ref: str, message: str) -> str | None: ...
    def restore(self, commit: str) -> bool: ...
    def delete(self, ref: str) -> None: ...


class NullSnapshotter:
    """No-op snapshotter: checkpoints carry no file state."""

    def capture(self, ref: str, message: str) -> str | None:
        return None

    def restore(self, commit: str) -> bool:
        return False  # nothing to restore; never reached (capture returns None)

    def delete(self, ref: str) -> None:
        pass


logger = logging.getLogger(__name__)

_REF_PREFIX = "refs/marim/checkpoints"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RewindResult:
    history_len: int
    restored_files: bool         # files were actually restored from the snapshot
    restore_failed: bool = False  # a file restore was attempted but git failed
    # The pre-rewind working tree, captured so the rewind is undoable; None when
    # the checkpoint had no file state (conversation-only) or capture failed.
    pre_restore_commit: str | None = None


class CheckpointManager:
    """Owns one session's checkpoint list. Captures a checkpoint at the start
    of each turn and rewinds the session (conversation + files) to one.

    Persistence is a sidecar JSON next to the session file; with no store the
    list lives only in memory. The git side is delegated to the injected
    ``Snapshotter`` (Null by default → conversation-only rewind)."""

    def __init__(
        self, session: SessionController, snapshotter: Snapshotter | None = None, *, limit: int = 50
    ) -> None:
        self.session = session
        self.snapshotter: Snapshotter = snapshotter or NullSnapshotter()
        self.limit = limit
        self._checkpoints: list[Checkpoint] = []
        # Commit of the most recent pre-rewind safety snapshot, so undo_rewind can
        # restore the working tree to its state just before the last rewind.
        self._pre_restore_commit: str | None = None
        # The conversation history stashed just before the last rewind truncated it,
        # so undo_rewind can put the conversation back. History — unlike the working
        # tree — has no shadow commit, so without this stash a rewind's truncation
        # would be irreversible.
        self._pre_rewind_history: list | None = None
        # Checkpoints a rewind dropped (index > the rewind target). Kept — with their
        # git refs alive — until the undo window closes, so undo_rewind can restore
        # them. Without this, rewinding to #3 then undoing brought the conversation
        # back but left checkpoints #4+ (and their snapshots) gone for good. In-memory
        # only, like the history stash above (a process restart still loses undo).
        self._pre_rewind_checkpoints: list[Checkpoint] | None = None
        # Session id recorded when the stash above was created. The undo stash's
        # git refs live under that id's namespace; by the time reload() runs on a
        # session switch the store already points at the NEW session, so reaping
        # the abandoned refs needs the id captured at stash time, not the current
        # one.
        self._stash_session_id: str | None = None
        self.reload()

    # --- persistence -----------------------------------------------------

    def _store(self) -> SessionStore | None:
        return self.session.store

    def _sidecar_path(self) -> Path | None:
        store = self._store()
        if store is None:
            return None
        return Path(store.path).with_name(f"{store.session_id}.checkpoints.json")

    def _save(self) -> None:
        path = self._sidecar_path()
        if path is None:
            return
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            payload = {"checkpoints": [c.to_dict() for c in self._checkpoints]}
            # Serialize the write behind the same advisory lock the sibling
            # session-state writers use (store.py, memory.py), so two processes on
            # the same session id don't race their sidecar writes bare. (The write
            # is already torn-safe via atomic_write_text's mkstemp+replace; the lock
            # brings it in line with the other locked writers. The residual
            # last-writer-wins between two processes sharing a session id is the
            # separate unnamed-session id-collision finding.)
            with file_lock(path):
                atomic_write_text(path, json.dumps(payload))
        except OSError as exc:
            logger.debug("failed to persist checkpoints: %s", exc)

    def reload(self) -> None:
        """Load the checkpoint list for the current session (called on
        resume/switch/new). A missing or corrupt sidecar yields an empty list."""
        self._checkpoints = []
        # Abandon the in-memory undo stash (it belongs to the session we're
        # leaving) and REAP its git refs — they are NOT re-derivable from the old
        # session's sidecar: rewind() saved that sidecar without the dropped
        # checkpoints, and the _pre_restore/_pre_undo safety snapshots are
        # recorded in no sidecar at all, so nothing but a full session delete()
        # would ever free them. Those are whole-working-tree captures (untracked
        # files, potentially secrets) that would otherwise stay pinned in .git
        # indefinitely. The refs are addressed under the session id recorded at
        # stash time (_reap_stash_refs) — _discard_undo_stash would target the
        # current (post-switch) session id, deleting the wrong namespace's refs.
        self._reap_stash_refs()
        path = self._sidecar_path()
        if path is None or not path.exists():
            return
        try:
            data = json.loads(path.read_text())
        except (OSError, json.JSONDecodeError) as exc:
            logger.debug("ignoring unreadable checkpoint sidecar %s: %s", path, exc)
            return
        self._checkpoints = [
            Checkpoint.from_dict(d) for d in data.get("checkpoints", [])
        ]

    # --- operations ------------------------------------------------------

    def _session_id(self) -> str:
        store = self._store()
        return store.session_id if store is not None else "anon"

    def _ref(self, index: int) -> str:
        return f"{_REF_PREFIX}/{self._session_id()}/{index}"

    def _pre_restore_ref(self) -> str:
        """Per-session ref for the pre-rewind safety snapshot. Namespacing by
        session id keeps a rewind in one session from clobbering another's
        recovery point (the bug behind the old shared ``_pre_restore`` ref)."""
        return f"{_REF_PREFIX}/{self._session_id()}/_pre_restore"

    def _pre_undo_ref(self) -> str:
        """Per-session ref for the safety snapshot taken just before an undo's file
        restore. Without it, undo_rewind's restore would delete any file created
        after the rewind (files present now but absent from the pre-restore tree)
        with no recovery path. Capturing the current tree here makes undo itself
        recoverable, mirroring how rewind() guards its own restore."""
        return f"{_REF_PREFIX}/{self._session_id()}/_pre_undo"

    def _discard_undo_stash(self) -> None:
        """Close the undo window: the last rewind can no longer be undone, so delete
        the git refs of the checkpoints it dropped (now truly orphaned — they aren't in
        ``_checkpoints`` anymore) and clear every rewind stash. Called when the user
        moves forward (a new snapshot), rewinds again, or the session is cleared —
        anything that supersedes the pending undo. Uses the CURRENT session id, so it
        must not run across a session switch (see ``reload``, which reaps the stash
        refs under the session id recorded at stash time to avoid targeting the wrong
        session's namespace)."""
        if self._pre_rewind_checkpoints:
            for cp in self._pre_rewind_checkpoints:
                if cp.commit is not None:
                    self.snapshotter.delete(self._ref(cp.index))
        self._pre_rewind_checkpoints = None
        self._pre_rewind_history = None
        self._pre_restore_commit = None
        self._stash_session_id = None

    def _reap_stash_refs(self) -> None:
        """The switch-safe sibling of ``_discard_undo_stash``: delete the git refs
        the undo stash was keeping alive, addressed under the session id recorded
        when the stash was created — safe to run after the store has been rebound
        to another session. Reaps only refs no sidecar references: the dropped
        checkpoints were removed from their sidecar by rewind()'s save (and if
        undo_rewind put them back, the stash list is already None so they are
        skipped), and the ``_pre_restore``/``_pre_undo`` safety snapshots are
        never recorded in any sidecar. delete() is a best-effort no-op on an
        absent ref, so the unconditional safety-ref deletes are safe."""
        sid = self._stash_session_id
        if sid is not None:
            if self._pre_rewind_checkpoints:
                for cp in self._pre_rewind_checkpoints:
                    if cp.commit is not None:
                        self.snapshotter.delete(f"{_REF_PREFIX}/{sid}/{cp.index}")
            self.snapshotter.delete(f"{_REF_PREFIX}/{sid}/_pre_restore")
            self.snapshotter.delete(f"{_REF_PREFIX}/{sid}/_pre_undo")
        self._pre_rewind_checkpoints = None
        self._pre_rewind_history = None
        self._pre_restore_commit = None
        self._stash_session_id = None

    def snapshot(self, prompt_preview: str) -> int:
        """Capture a checkpoint of the current state before a turn runs. Returns
        the new checkpoint's index so the caller can ``discard`` it if the turn
        then fails without producing anything."""
        # Capturing a new checkpoint means the user moved forward from any prior
        # rewind, so its undo window closes here — the dropped checkpoints it was
        # holding for undo are released (and their refs freed before the new index,
        # which may reuse one of theirs, is captured).
        self._discard_undo_stash()
        index = (self._checkpoints[-1].index + 1) if self._checkpoints else 0
        commit = self.snapshotter.capture(
            self._ref(index), f"marim checkpoint {index}"
        )
        self._checkpoints.append(
            Checkpoint(
                index=index,
                history_len=len(self.session.history),
                commit=commit,
                created=_now(),
                prompt_preview=(prompt_preview or "")[:80],
            )
        )
        self._prune()
        self._save()
        return index

    def discard(self, index: int) -> bool:
        """Drop the checkpoint with ``index`` iff it is the most recent one,
        deleting its shadow ref. Used to roll back the checkpoint a turn captured
        at its start when that turn then failed without producing any model output
        — such a checkpoint is a dead rewind target (its preview is just the
        failed prompt, and rewinding to it lands right before a turn that did
        nothing). The bare prompt itself still persists via the resumable flush;
        only the useless checkpoint goes. Returns True if one was dropped. Only the
        last checkpoint is removable, so a stale index can't punch a hole mid-list."""
        if not self._checkpoints or self._checkpoints[-1].index != index:
            return False
        cp = self._checkpoints.pop()
        if cp.commit is not None:
            self.snapshotter.delete(self._ref(cp.index))
        self._save()
        return True

    def _prune(self) -> None:
        if len(self._checkpoints) <= self.limit:
            return
        dropped = self._checkpoints[: -self.limit]
        self._checkpoints = self._checkpoints[-self.limit :]
        for cp in dropped:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))

    def list(self) -> list[Checkpoint]:
        return list(self._checkpoints)

    def rewind(self, index: int) -> RewindResult:
        """Restore the session to checkpoint ``index``: restore files (if the
        checkpoint has a commit), truncate history, and drop later checkpoints.
        Raises ``KeyError`` if no checkpoint has that index.

        The pre-rewind conversation is stashed first so ``undo_rewind`` can recover
        it — a rewind's history truncation is otherwise irreversible (history, unlike
        the working tree, has no shadow commit). File restore stays best-effort: a
        git failure never blocks the conversation rewind, and is independently
        recoverable via the pre-restore safety snapshot."""
        cp = next((c for c in self._checkpoints if c.index == index), None)
        if cp is None:
            raise KeyError(index)
        # A new rewind supersedes any pending undo from a PRIOR rewind (undo is
        # single-level), so close that window first — its dropped checkpoints become
        # unrecoverable now and their refs are freed.
        self._discard_undo_stash()
        restored = False
        restore_failed = False
        pre_restore_commit: str | None = None
        if cp.commit is not None:
            # Safety net: snapshot the current working tree (under a per-session
            # ref) so the file restore is undoable, THEN restore. If that snapshot
            # can't be captured (git failure), refuse the destructive restore rather
            # than overwrite the working tree with no recovery path — report it as a
            # failed restore. restore() reports success too, so a failed git restore
            # is never dressed up as a clean one.
            pre_restore_commit = self.snapshotter.capture(
                self._pre_restore_ref(), "pre-restore safety snapshot"
            )
            self._pre_restore_commit = pre_restore_commit
            if pre_restore_commit is None:
                restore_failed = True
            else:
                restored = self.snapshotter.restore(cp.commit)
                restore_failed = not restored
        # Stash the conversation, THEN truncate. The best-effort restore above never
        # blocks this, so the conversation always rewinds — and the stash lets
        # undo_rewind put it back even when the file restore failed or was absent.
        # Record which session the stash (and its refs) belongs to, so a later
        # session switch can reap the refs under the right namespace (see
        # _reap_stash_refs — the store may be rebound before reload() runs).
        self._stash_session_id = self._session_id()
        self._pre_rewind_history = list(self.session.history)
        self.session.set_history(self.session.history[: cp.history_len])
        self.session.persist(force=True)
        # Stash the later checkpoints instead of deleting them: keep their git refs
        # alive so undo_rewind can restore them to the list (rewinding to #3 then
        # undoing must bring #4+ back, not lose them for good). Their refs are deleted
        # only when the undo window closes — a new snapshot, a later rewind, or clear
        # (see _discard_undo_stash), mirroring the leak-safety _prune provides, just
        # deferred past the undo window.
        self._pre_rewind_checkpoints = [c for c in self._checkpoints if c.index > index]
        self._checkpoints = [c for c in self._checkpoints if c.index <= index]
        self._save()
        return RewindResult(
            history_len=cp.history_len,
            restored_files=restored,
            restore_failed=restore_failed,
            pre_restore_commit=pre_restore_commit,
        )

    def undo_rewind(self) -> bool:
        """Undo the last rewind, restoring both the conversation and the working
        tree to their pre-rewind state. Returns True if anything was restored, False
        when there is nothing to undo (no rewind this session). The conversation is
        recovered from the stash captured by ``rewind`` (and re-persisted); files are
        recovered from the pre-restore safety snapshot, which is absent for a
        conversation-only rewind. Both stashes are consumed, so a second call is a
        no-op."""
        undone = False
        if self._pre_rewind_history is not None:
            self.session.set_history(self._pre_rewind_history)
            self.session.persist(force=True)
            self._pre_rewind_history = None
            undone = True
        if self._pre_restore_commit is not None:
            # Safety net: undo's restore deletes files present now but absent from
            # the pre-restore tree — i.e. any work created AFTER the rewind. Snapshot
            # the current tree first so that post-rewind work is itself recoverable;
            # if that snapshot can't be captured (git failure), refuse the
            # destructive restore rather than wipe the new work with no recovery
            # path. Mirrors how rewind() guards its own restore.
            pre_undo = self.snapshotter.capture(
                self._pre_undo_ref(), "pre-undo safety snapshot"
            )
            if pre_undo is not None:
                if self.snapshotter.restore(self._pre_restore_commit):
                    undone = True
            else:
                logger.debug(
                    "skipping undo file restore: pre-undo safety snapshot failed"
                )
            self._pre_restore_commit = None
        # Bring the dropped later checkpoints back into the list (their refs were kept
        # alive for exactly this), so after undoing a rewind the user can rewind to a
        # LATER point again. Merge and re-sort by index; skip any index a post-rewind
        # snapshot already reused.
        if self._pre_rewind_checkpoints is not None:
            existing = {c.index for c in self._checkpoints}
            self._checkpoints.extend(
                c for c in self._pre_rewind_checkpoints if c.index not in existing
            )
            self._checkpoints.sort(key=lambda c: c.index)
            self._pre_rewind_checkpoints = None
            self._save()
            undone = True
        return undone

    def _delete_all_refs(self) -> None:
        """Delete every checkpoint's git ref and clear the list. Also closes any open
        undo window, since its stashed dropped checkpoints (not in ``_checkpoints``)
        would otherwise leak their refs — and reaps the per-session ``_pre_restore``
        /``_pre_undo`` safety snapshots. Those are whole-working-tree captures
        (untracked files, potentially secrets); ``_discard_undo_stash`` only clears
        the in-memory commit handle, so without deleting the refs here a ``clear()``
        /compaction-invalidate would leave the snapshots reachable in ``.git`` until
        a full session ``delete()``. delete() is best-effort — a no-op on an absent
        ref (a session that never rewound), so the unconditional deletes are safe.
        Uses the CURRENT session id, like ``_discard_undo_stash``; safe because the
        two callers (clear / invalidate_after_compaction) never run across a
        session switch."""
        self._discard_undo_stash()
        for cp in self._checkpoints:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))
        self.snapshotter.delete(self._pre_restore_ref())
        self.snapshotter.delete(self._pre_undo_ref())
        self._checkpoints = []

    def invalidate_after_compaction(self) -> None:
        """Drop every checkpoint after a compaction restructured the history.

        ``Checkpoint.history_len`` is an *absolute* index into the session
        history, but compaction replaces that history with a shorter, summarized
        list — so every existing checkpoint's index now points at a different
        (wrong) boundary. Rewinding to one would slice ``history[:stale_len]``
        mid-pair, stranding a tool call from its return or keeping a user message
        without its response, exactly the corruption that yields a malformed
        history on the next request. Those messages no longer exist in the same
        form (the prefix was collapsed into a summary), so the checkpoints are
        genuinely unrewindable and are dropped — along with their shadow refs, so
        they don't leak and block GC (mirrors ``_prune``).

        The current turn re-snapshots *after* compaction (see ``run_turn``), so
        this never discards the in-flight turn's rewind point — only the stale
        pre-compaction ones."""
        self._delete_all_refs()
        self._save()

    def clear(self) -> None:
        """Drop all checkpoints (called on session reset/clear) and their refs."""
        self._delete_all_refs()
        path = self._sidecar_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("failed to clear checkpoint sidecar: %s", exc)
