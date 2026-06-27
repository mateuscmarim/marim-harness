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

from ..atomic_io import atomic_write_text

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
        # Commit of the safety snapshot captured just before the last undo's file
        # restore, so a redo/recovery has a baseline for the post-rewind work that
        # undo would otherwise overwrite.
        self._pre_undo_commit: str | None = None
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
            atomic_write_text(path, json.dumps(payload))
        except OSError as exc:
            logger.debug("failed to persist checkpoints: %s", exc)

    def reload(self) -> None:
        """Load the checkpoint list for the current session (called on
        resume/switch/new). A missing or corrupt sidecar yields an empty list."""
        self._checkpoints = []
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

    def snapshot(self, prompt_preview: str) -> int:
        """Capture a checkpoint of the current state before a turn runs. Returns
        the new checkpoint's index so the caller can ``discard`` it if the turn
        then fails without producing anything."""
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
        self._pre_rewind_history = list(self.session.history)
        self.session.set_history(self.session.history[: cp.history_len])
        self.session.persist(force=True)
        # Drop the now-orphaned later checkpoints AND delete their git refs, so
        # refs/marim/checkpoints/... doesn't leak and block GC (mirrors _prune).
        for c in self._checkpoints:
            if c.index > index and c.commit is not None:
                self.snapshotter.delete(self._ref(c.index))
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
            self._pre_undo_commit = pre_undo
            if pre_undo is not None:
                if self.snapshotter.restore(self._pre_restore_commit):
                    undone = True
            else:
                logger.debug(
                    "skipping undo file restore: pre-undo safety snapshot failed"
                )
            self._pre_restore_commit = None
        return undone

    def _delete_all_refs(self) -> None:
        """Delete every checkpoint's git ref and clear the list."""
        for cp in self._checkpoints:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))
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
