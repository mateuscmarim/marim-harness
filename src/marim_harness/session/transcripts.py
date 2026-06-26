"""Per-sub-agent transcript sidecars.

A sub-agent's full step-by-step transcript is immutable once it finishes, but the
session JSON is re-serialized every turn — so transcripts live in write-once
sidecar files next to the session, loaded lazily only when a resumed pane is
opened. One file per spawn, keyed by the spawn's tool_call_id."""

from __future__ import annotations

import json
import logging
import re
import shutil
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter

from ..atomic_io import atomic_write_text
from ..workspace import cap_transcript

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")


def _safe(stream_id: str) -> str:
    """A filesystem-safe filename stem for a tool_call_id (same sanitization rule
    as the TUI's pane_id), prefixed to guarantee a non-empty, letter-leading name."""
    return "t-" + _UNSAFE.sub("-", stream_id or "none")


class TranscriptStore:
    """Reads/writes one sub-agent transcript per spawn under
    ``<session_path.parent>/<session_id>.subagents/<safe id>.json``. All methods
    are best-effort: a write/read failure logs and degrades, never raising into a
    turn or a resume."""

    def __init__(self, session_path, session_id: str) -> None:
        self._dir = Path(session_path).parent / f"{session_id}.subagents"

    def _file(self, stream_id: str) -> Path:
        return self._dir / f"{_safe(stream_id)}.json"

    def write(self, stream_id: str, messages: list, cap: int) -> None:
        if not stream_id or not messages:
            return
        try:
            capped = cap_transcript(messages, cap)
            payload = ModelMessagesTypeAdapter.dump_json(capped).decode("utf-8")
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(stream_id), payload)
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to write sub-agent transcript %s: %s", stream_id, exc)

    def read(self, stream_id: str) -> list | None:
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            return list(ModelMessagesTypeAdapter.validate_python(raw))
        except Exception as exc:  # noqa: BLE001 - a corrupt sidecar must not crash resume
            logger.warning("Failed to read sub-agent transcript %s: %s", stream_id, exc)
            return None

    def delete_all(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
