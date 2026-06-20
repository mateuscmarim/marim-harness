import hashlib
import json
import logging
import os
import re
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage

from ..images import externalize_images, rehydrate_images

logger = logging.getLogger(__name__)


def _default_base_dir() -> Path:
    base = os.environ.get("XDG_DATA_HOME") or str(Path.home() / ".local" / "share")
    return Path(base) / "marim-harness" / "sessions"


def _workspace_dir(base: Path, workspace_root: Path) -> Path:
    """A per-workspace directory holding one JSON file per named session."""
    digest = hashlib.sha256(str(workspace_root).encode()).hexdigest()[:12]
    return Path(base) / f"{workspace_root.name}-{digest}"


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "session"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _now_slug() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")


def _total_tokens(tok: dict) -> int:
    return tok.get("input", 0) + tok.get("output", 0)


@dataclass
class SessionInfo:
    """Lightweight summary of a saved session, for listing and picking."""

    id: str
    name: str
    updated: str
    message_count: int
    tokens: int
    duration_seconds: Optional[float] = None


class SessionStore:
    """Persists one named conversation to a JSON file, so it can be resumed
    across launches. Created by a :class:`SessionManager`, which decides the
    path, id, and name."""

    def __init__(self, path, workspace_root, session_id: str, name: str,
                 auto_named: bool = False, model: Optional[str] = None) -> None:
        self.path = Path(path)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = session_id
        self.name = name
        # True while the name is a placeholder eligible for automatic titling.
        self.auto_named = auto_named
        # The model id this session was last using (None -> the env default).
        self.model = model

    def save(self, history: list, usage: RunUsage,
             tasks: Optional[list] = None,
             duration_seconds: Optional[float] = None) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "id": self.session_id,
            "name": self.name,
            "auto": self.auto_named,
            "model": self.model,
            "workspace": str(self.workspace_root),
            "updated": _now(),
            "duration_seconds": duration_seconds,
            "tokens": {
                "input": usage.input_tokens,
                "output": usage.output_tokens,
                "requests": usage.requests,
                "cache_read": usage.cache_read_tokens,
                "cache_write": usage.cache_write_tokens,
                "cache_audio_read": usage.cache_audio_read_tokens,
                "input_audio": usage.input_audio_tokens,
                "output_audio": usage.output_audio_tokens,
                "tool_calls": usage.tool_calls,
                "details": usage.details,
            },
            "tasks": tasks or [],
        }
        messages_json = json.loads(ModelMessagesTypeAdapter.dump_json(history))
        messages_json = externalize_images(messages_json, self.session_id)
        payload["messages"] = messages_json
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(payload))
        tmp.replace(self.path)  # atomic swap so a crash mid-write can't corrupt

    def load(self) -> tuple[list, RunUsage, list, Optional[float]]:
        """Return ``(messages, usage, tasks, duration_seconds)``. Files written
        before task/duration tracking simply have no key and load as defaults."""
        if not self.path.exists():
            return [], RunUsage(), [], None
        data = json.loads(self.path.read_text())
        raw_messages = rehydrate_images(data.get("messages", []), self.session_id)
        messages = ModelMessagesTypeAdapter.validate_python(raw_messages)
        tok = data.get("tokens", {})
        # Old files predate the extra fields, so each defaults to 0 / {}.
        usage = RunUsage(
            input_tokens=tok.get("input", 0),
            output_tokens=tok.get("output", 0),
            requests=tok.get("requests", 0),
            cache_read_tokens=tok.get("cache_read", 0),
            cache_write_tokens=tok.get("cache_write", 0),
            cache_audio_read_tokens=tok.get("cache_audio_read", 0),
            input_audio_tokens=tok.get("input_audio", 0),
            output_audio_tokens=tok.get("output_audio", 0),
            tool_calls=tok.get("tool_calls", 0),
            details=tok.get("details") or {},
        )
        return messages, usage, data.get("tasks", []), data.get("duration_seconds")

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class SessionManager:
    """Owns the named sessions for one workspace: lists them, opens an existing
    one, and creates new ones with unique ids."""

    def __init__(self, workspace_root, base_dir: Optional[Path] = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self.dir = _workspace_dir(base, self.workspace_root)
        # Ids handed out this process but not yet written to disk, so two
        # create() calls in a row can't collide before the first save.
        self._reserved: set[str] = set()

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def list(self) -> list[SessionInfo]:
        """All saved sessions for this workspace, newest first."""
        infos: list[SessionInfo] = []
        if not self.dir.exists():
            return infos
        for path in self.dir.glob("*.json"):
            try:
                data = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError) as exc:
                logger.debug("skipping corrupt session file %s: %s", path, exc)
                continue
            infos.append(
                SessionInfo(
                    id=data.get("id", path.stem),
                    name=data.get("name", path.stem),
                    updated=data.get("updated", ""),
                    message_count=len(data.get("messages", [])),
                    tokens=_total_tokens(data.get("tokens", {})),
                    duration_seconds=data.get("duration_seconds"),
                )
            )
        infos.sort(key=lambda info: info.updated, reverse=True)
        return infos

    def store(self, session_id: str, name: Optional[str] = None) -> SessionStore:
        """Open the store for an existing (or known) session id. Recovers the
        display name and auto-named flag from the saved file when present."""
        path = self._path(session_id)
        saved = None
        if path.exists():
            try:
                saved = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                saved = None
        if name is None:
            name = str((saved or {}).get("name") or session_id)
        auto_named = bool((saved or {}).get("auto", False))
        model = (saved or {}).get("model")
        self._reserved.add(session_id)
        return SessionStore(
            path, self.workspace_root, session_id, name,
            auto_named=auto_named, model=model,
        )

    def create(self, name: Optional[str] = None) -> SessionStore:
        """Start a new session. The id is a slug of ``name`` (or a timestamp);
        the display name is ``name`` verbatim (or that timestamp slug). An
        unnamed session is flagged auto_named so it can be titled later.

        The new session inherits the model from the most recent session when
        available, so the user doesn't have to re-select it every time."""
        if name:
            base_slug = _slugify(name)
            display = name
            auto = False
        else:
            base_slug = _now_slug()
            display = base_slug
            auto = True
        store = self.store(self._unique_id(base_slug), display)
        store.auto_named = auto
        # Inherit the model from the most recent session when none is set yet.
        if store.model is None:
            store.model = self.latest_model()
        return store

    def _unique_id(self, base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in self._reserved or self._path(candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def latest(self) -> Optional[SessionInfo]:
        infos = self.list()
        return infos[0] if infos else None

    def latest_model(self) -> Optional[str]:
        """Return the model id of the most recent session, or *None*."""
        latest = self.latest()
        if latest is None:
            return None
        path = self._path(latest.id)
        try:
            data = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.debug("failed to read latest session for model: %s", exc)
            return None
        return data.get("model")

    def delete(self, session_id: str) -> None:
        self._path(session_id).unlink(missing_ok=True)
