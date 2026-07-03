"""Session-bound persistence for a spawn's sidecar transcript and terminal meta.

The runner delegates every transcript write/read and the terminal-meta stamping
to a ``SpawnTranscripts`` so the run loop stays about *running* spawns, not about
where their record lands. The store is read off the session controller on every
call — never cached — so a mid-flight ``/switch`` persists into the session that
is active at write time (the same rule the rest of the runner follows). Every
write is best-effort: a failure logs and degrades, never raising into a turn.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from ..session.ctrl import SessionController

logger = logging.getLogger(__name__)


def count_tool_calls(messages: list) -> int:
    """The number of tool calls in a spawn's transcript — the same tally the live
    card counts one ``note_tool`` at a time, recomputed from the persisted record
    for the terminal sidecar meta."""
    from pydantic_ai.messages import ModelResponse, ToolCallPart

    return sum(
        1
        for m in messages if isinstance(m, ModelResponse)
        for p in m.parts if isinstance(p, ToolCallPart)
    )


class SpawnTranscripts:
    """Persists sub-agent transcripts for one runner, bound to its *current*
    session. Holds the session controller (not a fixed store) so reads and writes
    always target the session active right now."""

    def __init__(self, session: SessionController, cap: int) -> None:
        self._session = session
        self._cap = cap

    @property
    def has_store(self) -> bool:
        """Whether a session store exists to persist into. The runner's resume path
        distinguishes 'no store at all' from 'no transcript for this spawn'."""
        return self._session.store is not None

    def _store(self):
        """A TranscriptStore bound to the *current* session (follows switches),
        or None when there's no session store."""
        store = self._session.store
        if store is None:
            return None
        from ..session import TranscriptStore
        return TranscriptStore(store.path, store.session_id)

    def save(self, stream_id: str, messages: list, meta: dict | None = None, *,
             cap_reasoning: bool = False) -> None:
        """Persist one spawn's transcript (best-effort). No-op without a store, an
        empty ``stream_id``, or empty ``messages``."""
        try:
            store = self._store()
            if stream_id and messages and store is not None:
                store.write(stream_id, messages, self._cap, meta=meta,
                            cap_reasoning=cap_reasoning)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to save transcript %s: %s", stream_id, exc)

    def read(self, stream_id: str) -> list | None:
        """The persisted messages for a spawn, or None (missing store or sidecar)."""
        store = self._store()
        return store.read(stream_id) if store is not None else None

    def read_meta(self, stream_id: str) -> dict | None:
        """The v2 sidecar meta for a spawn, or None (missing store or sidecar)."""
        store = self._store()
        return store.read_meta(stream_id) if store is not None else None

    def final_meta(self, template: dict | None, status: str, usage, t0: float,
                   messages: list | None = None) -> dict | None:
        """The terminal sidecar meta for a finished spawn: the ``template`` stamped
        with its terminal status, total spend, and run stats (tool tally +
        wall-clock duration measured from ``t0``) so a resumed session can rehydrate
        the sub-agents screen's columns. None when the spawn had no template
        (headless) — the sidecar then stays v1. The template is copied, never
        mutated (callers reuse it across mid-run checkpoints)."""
        if template is None:
            return None
        meta = dict(template)
        meta["status"] = status
        if usage is not None:
            meta["usage"] = {"input": usage.input_tokens, "output": usage.output_tokens}
        if messages is not None:
            meta["tool_count"] = count_tool_calls(messages)
        meta["duration"] = time.perf_counter() - t0
        return meta
