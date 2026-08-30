"""Where the running daemon is listening, published for local clients.

The token file and the workspace registry both persist under the server state
dir, but nothing recorded the bind address, so a client on the same machine had
no way to find the daemon without being told. ``runtime.json`` closes that: it
is written at startup and removed on clean exit.

Stdlib only, and deliberately outside the starlette-importing modules — a local
TUI reads this on machines that never installed the ``[serve]`` extra.

Presence of the file is a hint, not proof of life: a ``SIGKILL``ed daemon leaves
it behind. Treat a connection failure as the authoritative answer.
"""

import contextlib
import json
import os
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

from ..atomic_io import atomic_write_text


@dataclass(frozen=True)
class DaemonRuntime:
    host: str
    port: int
    pid: int
    started: str

    @property
    def url(self) -> str:
        return f"http://{self.host}:{self.port}"


def runtime_path(state_dir) -> Path:
    return Path(state_dir) / "runtime.json"


def write_runtime(state_dir, *, host: str, port: int) -> Path:
    """Record this process as the live daemon. Overwrites any stale file."""
    path = runtime_path(state_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(
        path,
        json.dumps(
            {
                "host": host,
                "port": port,
                "pid": os.getpid(),
                "started": datetime.now(timezone.utc).isoformat(),
            },
            indent=2,
        ),
    )
    return path


def read_runtime(state_dir) -> DaemonRuntime | None:
    """The recorded daemon, or None if absent or unreadable."""
    try:
        data = json.loads(runtime_path(state_dir).read_text())
    except (OSError, ValueError):
        return None
    try:
        return DaemonRuntime(
            host=str(data["host"]),
            port=int(data["port"]),
            pid=int(data["pid"]),
            started=str(data["started"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def clear_runtime(state_dir) -> None:
    """Remove OUR record on clean shutdown, leaving another daemon's alone.

    Ownership-checked on purpose: a second ``marim serve`` that fails to start
    (a port already in use exits through this same shutdown path) must not
    delete the record of the daemon that is still running and still serving.
    So we unlink only when the record on disk names this process; anything
    else — no record, or someone else's — is left untouched. Never raises.
    """
    record = read_runtime(state_dir)
    if record is None or record.pid != os.getpid():
        return
    with contextlib.suppress(OSError):
        runtime_path(state_dir).unlink(missing_ok=True)
