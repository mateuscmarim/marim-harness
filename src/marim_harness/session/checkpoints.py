"""Per-session checkpoints: a capture of conversation length + an optional
shadow git commit of the working tree, taken at the start of each turn so a
session can be rewound to an earlier point.

The git work is injected as a ``Snapshotter`` so this module stays
git-agnostic and unit-testable; the real implementation lives in
``workspace/snapshot.py``."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional, Protocol


@dataclass
class Checkpoint:
    index: int            # monotonic ordinal, unique within a session
    history_len: int      # len(history) captured before this turn ran
    commit: Optional[str] # shadow commit sha (restore target), or None
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
    def from_dict(cls, d: dict) -> "Checkpoint":
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

    def capture(self, ref: str, message: str) -> Optional[str]: ...
    def restore(self, commit: str) -> None: ...
    def delete(self, ref: str) -> None: ...


class NullSnapshotter:
    """No-op snapshotter: checkpoints carry no file state."""

    def capture(self, ref: str, message: str) -> Optional[str]:
        return None

    def restore(self, commit: str) -> None:
        pass

    def delete(self, ref: str) -> None:
        pass
