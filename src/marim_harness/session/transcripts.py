"""Per-sub-agent transcript sidecars.

A sub-agent's step-by-step transcript would otherwise live only in the session
JSON, which is re-serialized every turn — so it instead lives in its own sidecar
file next to the session, loaded lazily only when a resumed pane is opened. A
sidecar is no longer write-once: a running spawn checkpoints it (v2 envelope,
``status="running"``) before every model request, so a process death mid-run
still leaves a resumable trail, and the spawn's completion (or failure) stamps a
terminal status on the final write. One file per spawn, keyed by the spawn's
tool_call_id."""

from __future__ import annotations

import hashlib
import json
import logging
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

from pydantic_ai.messages import ModelMessagesTypeAdapter

from ..atomic_io import atomic_write_text
from ..images import externalize_images, rehydrate_images
from ..workspace import cap_transcript

logger = logging.getLogger(__name__)

_UNSAFE = re.compile(r"[^a-zA-Z0-9_.-]")


def _safe(stream_id: str) -> str:
    """A filesystem-safe, INJECTIVE filename stem for a tool_call_id, prefixed to
    guarantee a non-empty, letter-leading name.

    The readable prefix (the sanitized id) is not injective on its own — distinct
    ids that differ only in unsafe characters collapse together (``a/b`` and
    ``a b`` both sanitize to ``a-b``), so one spawn's sidecar would silently
    overwrite another's. Appending a short hash of the TRUE id restores
    injectivity: distinct ids always yield distinct stems, while the prefix stays
    human-scannable in the directory listing."""
    raw = stream_id or "none"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:12]
    return "t-" + _UNSAFE.sub("-", raw) + "-" + digest


class TranscriptStore:
    """Reads/writes one sub-agent transcript per spawn under
    ``<session_path.parent>/<session_id>.subagents/<safe id>.json``. All methods
    are best-effort: a write/read failure logs and degrades, never raising into a
    turn or a resume."""

    def __init__(self, session_path, session_id: str) -> None:
        self._dir = Path(session_path).parent / f"{session_id}.subagents"
        # Keyed the same as the parent session's own image externalization, so
        # an image that appears in both the session JSON and a spawn's sidecar
        # dedupes to one content-addressed cache file.
        self._session_id = session_id

    def _file(self, stream_id: str) -> Path:
        return self._dir / f"{_safe(stream_id)}.json"

    def write(self, stream_id: str, messages: list, cap: int,
              meta: dict | None = None, *, cap_reasoning: bool = False) -> None:
        """Persist one spawn's transcript. With ``meta`` the file is a v2 envelope
        ``{"v": 2, "meta": ..., "messages": [...]}`` — the meta carries what a
        resumed session needs to rebuild the card and (for an interrupted spawn)
        re-run it. Without ``meta`` the historical v1 bare-list format is kept, so
        callers migrate incrementally and old files stay valid. ``meta`` must carry
        ``stream_id``: the filename is a lossy sanitization, so ``scan_meta`` can
        only key results off the id stored inside the file.

        ``cap_reasoning`` (checkpoint path only) also clips oversized text/thinking
        parts to ``cap`` so a mid-run sidecar re-written before every model request
        stays bounded — see ``cap_transcript``."""
        if not stream_id or not messages:
            return
        try:
            capped = cap_transcript(messages, cap, cap_reasoning=cap_reasoning)
            msgs = ModelMessagesTypeAdapter.dump_python(capped, mode="json")
            # Swap inline base64 for cache refs, exactly like the session JSON
            # (session/store.py): a 5 MB image read would otherwise write ~6.7 MB
            # of base64 here, and the mid-run checkpoint rewrites this file
            # before EVERY model request of the spawn. Safe to mutate: dump_python
            # produced fresh dicts, so the live run's messages are untouched.
            msgs = externalize_images(msgs, self._session_id)
            if meta is None:
                payload = msgs
            else:
                meta = dict(meta)  # never mutate the caller's (reused) meta dict
                meta["stream_id"] = stream_id
                meta["updated"] = datetime.now(timezone.utc).isoformat()
                payload = {"v": 2, "meta": meta, "messages": msgs}
            self._dir.mkdir(parents=True, exist_ok=True)
            atomic_write_text(self._file(stream_id), json.dumps(payload))
        except Exception as exc:  # noqa: BLE001 - persistence is best-effort
            logger.warning("Failed to write sub-agent transcript %s: %s", stream_id, exc, exc_info=True)

    def read(self, stream_id: str) -> list | None:
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
            if isinstance(raw, dict):  # v2 envelope; a bare list is a v1 file
                raw = raw.get("messages", [])
            # A missing cache file degrades that one image to a placeholder
            # string (never a read failure) — same contract as session load.
            raw = rehydrate_images(raw, self._session_id)
            return list(ModelMessagesTypeAdapter.validate_python(raw))
        except Exception as exc:  # noqa: BLE001 - a corrupt sidecar must not crash resume
            logger.warning("Failed to read sub-agent transcript %s: %s", stream_id, exc, exc_info=True)
            return None

    def has_transcript(self, stream_id: str) -> bool:
        """Whether ANY sidecar exists for this spawn — v1 or v2 — without reading
        it. The resume settle uses this to tell a legacy v1 spawn (pre-envelope
        file: ran and completed, but invisible to ``scan_meta``) apart from a
        spawn that never executed at all (no file)."""
        return bool(stream_id) and self._file(stream_id).exists()

    def read_meta(self, stream_id: str) -> dict | None:
        """The v2 meta for one spawn, without validating its messages (cheap).
        None for a missing, corrupt, or v1 (bare-list) sidecar."""
        path = self._file(stream_id)
        if not path.exists():
            return None
        try:
            raw = json.loads(path.read_text())
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to read sidecar meta %s: %s", stream_id, exc, exc_info=True)
            return None
        if isinstance(raw, dict) and isinstance(raw.get("meta"), dict):
            return raw["meta"]
        return None

    def scan_meta(self) -> dict[str, dict]:
        """stream_id → meta for every v2 sidecar in this session's dir. Used once
        at session resume to find spawns that died mid-run (meta still says
        ``running``). Corrupt and v1 files are skipped with a warning — detection
        degrades to fewer interrupted cards, never a crash."""
        out: dict[str, dict] = {}
        if not self._dir.exists():
            return out
        for path in self._dir.glob("*.json"):
            try:
                raw = json.loads(path.read_text())
            except Exception as exc:  # noqa: BLE001
                logger.warning("Skipping unreadable sidecar %s: %s", path, exc, exc_info=True)
                continue
            if not isinstance(raw, dict):
                continue
            meta = raw.get("meta")
            if not isinstance(meta, dict):
                continue
            sid = meta.get("stream_id")
            if sid:
                out[str(sid)] = meta
        return out

    def delete_all(self) -> None:
        shutil.rmtree(self._dir, ignore_errors=True)
