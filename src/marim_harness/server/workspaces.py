"""Server-side workspace registry: named directories sessions run in.

Two flavors. *Registered* workspaces point at existing directories on the host
(like opening a project) and are never deleted from disk. *Managed* workspaces
are created by the server under a workspaces root — empty or git-cloned — and
may be purged on delete. Persisted as one JSON file under the server state
dir."""

import json
import logging
import re
import shutil
import subprocess
from contextlib import suppress
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text

logger = logging.getLogger(__name__)

_CLONE_TIMEOUT_SECONDS = 600


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _slugify(name: str) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return slug or "workspace"


@dataclass(frozen=True)
class WorkspaceRecord:
    id: str
    name: str
    path: str
    kind: str  # "registered" | "managed"
    created: str

    def as_dict(self) -> dict:
        return asdict(self)


class WorkspaceRegistry:
    def __init__(self, state_file: Path, workspaces_root: Path) -> None:
        self._file = state_file
        self.workspaces_root = workspaces_root
        self._records: dict[str, WorkspaceRecord] = self._load()

    def _load(self) -> dict[str, WorkspaceRecord]:
        if not self._file.exists():
            return {}
        try:
            data = json.loads(self._file.read_text())
        except (json.JSONDecodeError, OSError) as exc:
            logger.warning("workspace registry state file unreadable, starting empty: %s", exc)
            return {}
        records = {}
        for raw in data.get("workspaces", []):
            record = WorkspaceRecord(**raw)
            records[record.id] = record
        return records

    def _save(self) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        payload = {"workspaces": [r.as_dict() for r in self._records.values()]}
        atomic_write_text(self._file, json.dumps(payload, indent=2))

    def list(self) -> list[WorkspaceRecord]:
        return list(self._records.values())

    def get(self, ws_id: str) -> WorkspaceRecord | None:
        return self._records.get(ws_id)

    def _unique_id(self, base: str) -> str:
        candidate = base
        suffix = 2
        while candidate in self._records or (self.workspaces_root / candidate).exists():
            candidate = f"{base}-{suffix}"
            suffix += 1
        return candidate

    def register(self, name: str, path: Path) -> WorkspaceRecord:
        resolved = path.expanduser().resolve()
        if not resolved.is_dir():
            raise ValueError(f"not a directory: {resolved}")
        record = WorkspaceRecord(
            id=self._unique_id(_slugify(name)), name=name, path=str(resolved),
            kind="registered", created=_now(),
        )
        self._records[record.id] = record
        self._save()
        return record

    def create_managed(self, name: str, git_url: str | None = None) -> WorkspaceRecord:
        ws_id = self._unique_id(_slugify(name))
        target = self.workspaces_root / ws_id
        target.mkdir(parents=True, exist_ok=False)
        if git_url is not None:
            try:
                subprocess.run(
                    ["git", "clone", git_url, str(target)],
                    check=True, capture_output=True, text=True,
                    timeout=_CLONE_TIMEOUT_SECONDS,
                )
            except (subprocess.CalledProcessError, subprocess.TimeoutExpired) as exc:
                shutil.rmtree(target, ignore_errors=True)
                detail = getattr(exc, "stderr", "") or str(exc)
                raise ValueError(f"git clone failed: {detail.strip()}") from exc
        record = WorkspaceRecord(
            id=ws_id, name=name, path=str(target.resolve()), kind="managed", created=_now(),
        )
        self._records[record.id] = record
        self._save()
        return record

    def delete(self, ws_id: str, *, purge: bool = False) -> None:
        record = self._records.get(ws_id)
        if record is None:
            raise KeyError(ws_id)
        if purge and record.kind != "managed":
            raise ValueError("purge applies only to managed workspaces")
        if purge:
            with suppress(FileNotFoundError):
                shutil.rmtree(record.path)
        del self._records[ws_id]
        self._save()
