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
from typing import Protocol


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
    def restore(self, commit: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class NullSnapshotter:
    """No-op snapshotter: checkpoints carry no file state."""

    def capture(self, ref: str, message: str) -> str | None:
        return None

    def restore(self, commit: str) -> None:
        pass

    def delete(self, ref: str) -> None:
        pass


logger = logging.getLogger(__name__)

_REF_PREFIX = "refs/marim/checkpoints"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class RewindResult:
    history_len: int
    restored_files: bool


class CheckpointManager:
    """Owns one session's checkpoint list. Captures a checkpoint at the start
    of each turn and rewinds the session (conversation + files) to one.

    Persistence is a sidecar JSON next to the session file; with no store the
    list lives only in memory. The git side is delegated to the injected
    ``Snapshotter`` (Null by default → conversation-only rewind)."""

    def __init__(
        self, session, snapshotter: Snapshotter | None = None, *, limit: int = 50
    ) -> None:
        self.session = session
        self.snapshotter: Snapshotter = snapshotter or NullSnapshotter()
        self.limit = limit
        self._checkpoints: list[Checkpoint] = []
        self.reload()

    # --- persistence -----------------------------------------------------

    def _sidecar_path(self) -> Path | None:
        store = getattr(self.session, "store", None)
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
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload))
            tmp.replace(path)  # atomic swap, mirrors SessionStore.save
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
        store = getattr(self.session, "store", None)
        return getattr(store, "session_id", "anon") if store is not None else "anon"

    def _ref(self, index: int) -> str:
        return f"{_REF_PREFIX}/{self._session_id()}/{index}"

    def snapshot(self, prompt_preview: str) -> None:
        """Capture a checkpoint of the current state before a turn runs."""
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
        """Restore the session to checkpoint ``index``: truncate history, restore
        files (if the checkpoint has a commit), and drop later checkpoints.
        Raises ``KeyError`` if no checkpoint has that index."""
        cp = next((c for c in self._checkpoints if c.index == index), None)
        if cp is None:
            raise KeyError(index)
        self.session.set_history(self.session.history[: cp.history_len])
        self.session.persist(force=True)
        restored = False
        if cp.commit is not None:
            self.snapshotter.restore(cp.commit)
            restored = True
        self._checkpoints = [c for c in self._checkpoints if c.index <= index]
        self._save()
        return RewindResult(history_len=cp.history_len, restored_files=restored)

    def clear(self) -> None:
        """Drop all checkpoints (called on session reset/clear) and their refs."""
        for cp in self._checkpoints:
            if cp.commit is not None:
                self.snapshotter.delete(self._ref(cp.index))
        self._checkpoints = []
        path = self._sidecar_path()
        if path is not None:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                logger.debug("failed to clear checkpoint sidecar: %s", exc)
