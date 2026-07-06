import hashlib
import json
import logging
import os
import re
import secrets
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from pydantic import ValidationError
from pydantic_ai.messages import ModelMessagesTypeAdapter
from pydantic_ai.usage import RunUsage

from ..atomic_io import atomic_write_text, file_lock
from ..images import externalize_images, rehydrate_images

logger = logging.getLogger(__name__)


class SessionLoadError(Exception):
    """A saved session file exists but can't be read (corrupt JSON, unreadable).

    Raised by :meth:`SessionStore.load` instead of leaking a raw
    ``JSONDecodeError``/``OSError`` traceback. ``list()`` deliberately *skips*
    such files (a corrupt sibling shouldn't break the picker), but a resume/switch
    targeting a specific session must fail loudly and namedly — silently loading an
    empty history would look like the conversation was lost."""


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


# How much of a session file the picker fast path reads. The header (id, name,
# updated, tokens, tasks, jobs, message_count) is normally well under this; a
# header that overflows it just falls back to the full parse.
_HEADER_PROBE_CHARS = 65536


def _header_fields(path: Path) -> dict | None:
    """Parse just the pre-``messages`` header of a session file, or None when
    the fast path can't apply (old layout, oversized header, unreadable file).

    ``save`` writes the messages array *last* precisely so picker rows never
    pay for parsing it — on a long session that array is multi-MB while the
    header is a few hundred bytes. The cut point is self-validating: the raw
    sequence ``, "messages":`` can't occur inside a JSON string (its quotes
    would be escaped), and a nested-dict occurrence sits at bracket depth ≥ 2,
    so closing the object there leaves unbalanced JSON that fails to parse and
    falls back to the full read. Requiring ``message_count`` keeps pre-header
    files (which would need the messages array anyway) on the fallback path."""
    try:
        with path.open(encoding="utf-8") as f:
            head = f.read(_HEADER_PROBE_CHARS)
    except (OSError, UnicodeDecodeError):
        return None
    idx = head.find(', "messages":')
    if idx == -1:
        return None
    try:
        data = json.loads(head[:idx] + "}")
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict) and "message_count" in data:
        return data
    return None


@dataclass
class SessionInfo:
    """Lightweight summary of a saved session, for listing and picking."""

    id: str
    name: str
    updated: str
    message_count: int
    tokens: int
    duration_seconds: float | None = None
    model: str | None = None


class SessionStore:
    """Persists one named conversation to a JSON file, so it can be resumed
    across launches. Created by a :class:`SessionManager`, which decides the
    path, id, and name."""

    def __init__(self, path, workspace_root, session_id: str, name: str,
                 auto_named: bool = False, model: str | None = None) -> None:
        self.path = Path(path)
        self.workspace_root = Path(workspace_root).resolve()
        self.session_id = session_id
        self.name = name
        # True while the name is a placeholder eligible for automatic titling.
        self.auto_named = auto_named
        # The model id this session was last using (None -> the env default).
        self.model = model

    def save(self, history: list, usage: RunUsage,
             tasks: list | None = None,
             duration_seconds: float | None = None,
             jobs: list | None = None) -> None:
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
            "jobs": jobs or [],
            # Cheap header field so SessionManager.list() can report the count
            # without parsing the (potentially multi-MB) messages array. Old
            # files predate it; list() falls back to len(messages) when absent.
            "message_count": len(history),
        }
        # dump_python(mode="json") yields the same jsonable structure as
        # json.loads(dump_json(...)) but skips one full serialize+parse round
        # trip (we still do a single json.dumps below to write the file).
        messages_json = ModelMessagesTypeAdapter.dump_python(history, mode="json")
        messages_json = externalize_images(messages_json, self.session_id)
        payload["messages"] = messages_json
        # Serialize same-session saves across processes (TUI + headless, or two
        # runs) with a best-effort advisory lock. Without it, two writers racing
        # on the same session_id last-writer-wins on os.replace and a whole
        # conversation can be silently overwritten.
        with file_lock(self.path):
            atomic_write_text(self.path, json.dumps(payload))

    def save_meta(self) -> None:
        """Patch this session's on-disk name/auto-named/model header without
        rewriting the messages array.

        ``save`` serializes the entire in-memory history, which is only safe at a
        moment the history is known-clean. A background rename (autoname) or a
        mid-turn ``/model`` switch can land mid-turn, when the in-memory history
        may end in unanswered tool calls that must never reach disk (see
        ``TurnController._run_with_approval``), so it patches just the metadata
        under the same advisory lock ``save`` takes — whichever writer runs
        second converges, because the in-memory fields are updated before either
        write. No-op when the file doesn't exist yet or is unreadable: the next
        full ``save`` carries the in-memory fields anyway."""
        with file_lock(self.path):
            try:
                data = json.loads(self.path.read_text())
            except (json.JSONDecodeError, OSError):
                return
            data["name"] = self.name
            data["auto"] = self.auto_named
            data["model"] = self.model
            atomic_write_text(self.path, json.dumps(data))

    def load(self) -> tuple[list, RunUsage, list, float | None, list]:
        """Return ``(messages, usage, tasks, duration_seconds, jobs)``. Files
        written before task/duration/jobs tracking simply have no key and load
        as defaults."""
        if not self.path.exists():
            return [], RunUsage(), [], None, []
        try:
            data = json.loads(self.path.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            # Don't return defaults here: unlike list() (which skips a corrupt
            # sibling), a caller asked to load *this* session, and a silent empty
            # would masquerade as data loss. Fail with a path the user can act on.
            raise SessionLoadError(
                f"can't read session {self.path}: {exc}. Move the file aside or "
                f"start a fresh session."
            ) from exc
        raw_messages = rehydrate_images(data.get("messages", []), self.session_id)
        try:
            messages = ModelMessagesTypeAdapter.validate_python(raw_messages)
        except ValidationError as exc:
            # Valid JSON whose messages no longer validate — a session written
            # by a different marim/pydantic-ai version. Same contract as the
            # corrupt-JSON branch above: fail loudly with an actionable path,
            # not a raw pydantic traceback.
            raise SessionLoadError(
                f"can't read session {self.path}: its messages don't match this "
                f"version's schema ({type(exc).__name__}). Move the file aside "
                f"or start a fresh session."
            ) from exc
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
        return (messages, usage, data.get("tasks", []),
                data.get("duration_seconds"), data.get("jobs", []))

    def clear(self) -> None:
        self.path.unlink(missing_ok=True)


class SessionManager:
    """Owns the named sessions for one workspace: lists them, opens an existing
    one, and creates new ones with unique ids."""

    def __init__(self, workspace_root, base_dir: Path | None = None) -> None:
        self.workspace_root = Path(workspace_root).resolve()
        base = Path(base_dir) if base_dir is not None else _default_base_dir()
        self.dir = _workspace_dir(base, self.workspace_root)
        # Ids handed out this process but not yet written to disk, so two
        # create() calls in a row can't collide before the first save.
        self._reserved: set[str] = set()

    def _path(self, session_id: str) -> Path:
        return self.dir / f"{session_id}.json"

    def session_path(self, session_id: str) -> Path:
        """Public path lookup for a session's JSON file (which may not exist
        yet). Read-only callers — the server's history endpoint — use this to
        read persisted messages without opening a SessionStore (whose
        construction reserves the id)."""
        return self._path(session_id)

    def list(self) -> list[SessionInfo]:
        """All saved sessions for this workspace, newest first."""
        infos: list[SessionInfo] = []
        if not self.dir.exists():
            return infos
        for path in self.dir.glob("*.json"):
            # Skip a session's checkpoint sidecar (``<id>.checkpoints.json``): it
            # shares the sessions dir and matches ``*.json`` but is NOT a session.
            # Killing marim during a session's first-ever turn (before the session
            # file is written) would otherwise leave only the checkpoint file, and
            # listing it as a phantom session lets ``--resume`` open the phantom —
            # hiding the real, interrupted session and its spawns.
            if path.name.endswith(".checkpoints.json"):
                continue
            # Fast path: parse only the header (see _header_fields) so listing
            # never deserializes every session's full messages array.
            data = _header_fields(path)
            if data is None:
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
                    # Prefer the cheap header count written by save(); fall back to
                    # counting the messages array for files written before it.
                    message_count=data.get(
                        "message_count", len(data.get("messages", []))
                    ),
                    tokens=_total_tokens(data.get("tokens", {})),
                    duration_seconds=data.get("duration_seconds"),
                    model=data.get("model"),
                )
            )
        infos.sort(key=lambda info: info.updated, reverse=True)
        return infos

    def store(self, session_id: str, name: str | None = None) -> SessionStore:
        """Open the store for an existing (or known) session id. Recovers the
        display name and auto-named flag from the saved file when present."""
        path = self._path(session_id)
        saved = None
        if path.exists():
            try:
                saved = json.loads(path.read_text())
            except (json.JSONDecodeError, OSError):
                saved = None
        meta = saved or {}
        if name is None:
            name = str(meta.get("name") or session_id)
        auto_named = bool(meta.get("auto", False))
        model = meta.get("model")
        self._reserved.add(session_id)
        return SessionStore(
            path, self.workspace_root, session_id, name,
            auto_named=auto_named, model=model,
        )

    def create(self, name: str | None = None) -> SessionStore:
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
            ts = _now_slug()
            display = ts
            # A bare second-resolution id collides when two processes start an
            # unnamed session in the same second: each manager's _reserved is
            # in-memory and the session file doesn't exist until the first save, so
            # _unique_id (which only checks _reserved + on-disk) hands both the same
            # id — and they then clobber each other's session JSON, checkpoints
            # sidecar, transcript dir, image cache, and git refs. A short random
            # token makes concurrent unnamed ids distinct while the display keeps
            # the clean timestamp. (_unique_id still guards the astronomically
            # unlikely same-token case.)
            base_slug = f"{ts}-{secrets.token_hex(3)}"
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

    def latest(self) -> SessionInfo | None:
        infos = self.list()
        return infos[0] if infos else None

    def latest_model(self) -> str | None:
        """Return the model id of the most recent session, or *None*."""
        latest = self.latest()
        return latest.model if latest is not None else None

    def delete(self, session_id: str) -> None:
        """Remove a session and every sidecar keyed by its id: the JSON file,
        the checkpoints sidecar, the sub-agent transcript dir, the image cache
        dir, and the ``refs/marim/checkpoints/<id>/*`` git refs (which pin
        whole-working-tree snapshot commits — untracked files included — in
        ``.git`` indefinitely). Each step is independent and best-effort, so a
        missing artifact never blocks removing the rest."""
        import shutil

        # Imported here, not at module top: transcripts imports from workspace,
        # and keeping store's top-level imports lean avoids widening the
        # session package's import surface for embedders that only list/save.
        from ..images import image_cache_root
        from ..workspace.snapshot import delete_checkpoint_refs
        from .transcripts import TranscriptStore

        self._path(session_id).unlink(missing_ok=True)
        with_suffix = self.dir / f"{session_id}.checkpoints.json"
        with_suffix.unlink(missing_ok=True)
        TranscriptStore(self._path(session_id), session_id).delete_all()
        shutil.rmtree(image_cache_root() / session_id, ignore_errors=True)
        delete_checkpoint_refs(self.workspace_root, session_id)
