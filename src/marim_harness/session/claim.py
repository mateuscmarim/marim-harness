"""Non-blocking flock-based session-ownership claims.

A claim marks the session a process is ACTIVELY DRIVING: ``try_acquire`` opens
(or creates) the ``<id>.json.claim`` sidecar, ``flock(LOCK_EX | LOCK_NB)`` it,
and writes the holder's identity into the LOCKED fd. The kernel releases the
lock on process death — no pid liveness checks, no stale-lock cleanup.

Ownership follows the active view: a process swaps claims when it switches
sessions (Harness.switch_session), so a claim is held for as long as its
holder drives that session — not necessarily the process's whole lifetime.

Degrade stance (accepted, mirrors atomic_io.file_lock): if the claim file
cannot be opened, the caller proceeds UNCLAIMED rather than refusing to run.
Locking is a safety net against two simultaneous owners under normal
operation, not an absolute guarantee under resource failure.

Naming invariant: the sidecar is ``<id>.json.claim`` — not ``.lock``
(atomic_io.file_lock's name; sharing it would block every SessionStore.save)
and not ``<id>.claim.json`` (SessionManager.list's *.json glob would render
it as a phantom session).
"""

import contextlib
import json
import logging
import os
from dataclasses import dataclass
from pathlib import Path

# POSIX-only, exactly as in atomic_io. Guarded so this module still imports on a
# platform without it, where claiming degrades to always-succeed (see try_acquire).
try:  # pragma: no cover - exercised by import on the running platform
    import fcntl
except ImportError:  # pragma: no cover - Windows / no-fcntl platforms
    fcntl = None  # type: ignore[assignment]

logger = logging.getLogger(__name__)

CLAIM_SUFFIX = ".claim"


def claim_path(session_path) -> Path:
    """The claim sidecar for a session file: ``<id>.json`` -> ``<id>.json.claim``."""
    path = Path(session_path)
    return path.with_name(path.name + CLAIM_SUFFIX)


@dataclass(frozen=True)
class Holder:
    """Who owns a claimed session — read for the refusal message only."""

    pid: int
    kind: str  # "tui" | "headless" | "daemon"
    endpoint: str | None

    def describe(self) -> str:
        where = f" at {self.endpoint}" if self.endpoint else ""
        return f"{self.kind} (pid {self.pid}){where}"


class SessionClaimed(Exception):
    """Raised when a session is owned by another live process.

    Not a transient condition to retry: the holder keeps the session until it
    exits (or switches away from it), so the caller's job is to report who has
    it, not to back off."""

    def __init__(self, session_id: str, holder: "Holder | None") -> None:
        super().__init__(session_id)
        self.session_id = session_id
        self.holder = holder


class SessionClaim:
    """A held claim. Release it (or leave the ``with`` block) to give up ownership.

    Releasing is best-effort and idempotent: the kernel drops the lock when the
    fd closes regardless, so a failed unlock can never strand a session.
    """

    def __init__(self, path: Path, fd: int | None, displaced: "Holder | None" = None) -> None:
        self.path = path
        self._fd = fd
        # Who the claim file described when this claim took it — the LAST
        # holder, long gone (the lock was free). None for a never-claimed
        # session, an unreadable file, or a degraded (unlocked) claim. The CLI
        # reads it to tell a user who expected to attach to a ``marim serve``
        # daemon that the daemon no longer holds the session.
        self.displaced = displaced

    def release(self) -> None:
        fd, self._fd = self._fd, None
        if fd is None:
            return
        if fcntl is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)

    def __enter__(self) -> "SessionClaim":
        return self

    def __exit__(self, *exc_info) -> None:
        self.release()


def try_acquire(session_path, *, kind: str, endpoint: str | None = None) -> SessionClaim | None:
    """Take ownership of ``session_path``, or return None if someone else has it.

    Never blocks. On a platform without ``fcntl`` this always succeeds with an
    unlocked claim — matching ``atomic_io.file_lock``'s degrade-to-no-op stance,
    since locking is a safety net and refusing to run at all would be worse.
    """
    path = claim_path(session_path)
    if fcntl is None:
        return SessionClaim(path, None)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(path, os.O_CREAT | os.O_RDWR, 0o644)
    except OSError as exc:
        logger.debug("claim file unavailable at %s, proceeding unclaimed: %s", path, exc)
        return SessionClaim(path, None)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        # Held by someone else. Close our fd (it holds no lock) and refuse.
        with contextlib.suppress(OSError):
            os.close(fd)
        return None
    # Read the previous holder BEFORE stamping ourselves over it: with the lock
    # ours, whatever the file says is a claim that has already ended.
    displaced = read_holder(session_path)
    _write_identity(fd, kind=kind, endpoint=endpoint)
    return SessionClaim(path, fd, displaced)


def _write_identity(fd: int, *, kind: str, endpoint: str | None) -> None:
    """Stamp who we are into the locked file, for a refused caller to report.

    Written straight to the locked fd — deliberately NOT via
    ``atomic_write_text``. That does ``os.replace``, which swaps in a new inode;
    our flock lives on the old one, so the next acquirer would open the new
    inode, lock it successfully, and both processes would believe they own the
    session. Best-effort: the identity is diagnostics, and failing to record it
    must not cost us the claim we already hold.
    """
    payload = json.dumps({"pid": os.getpid(), "kind": kind, "endpoint": endpoint})
    try:
        os.ftruncate(fd, 0)
        os.lseek(fd, 0, os.SEEK_SET)
        os.write(fd, payload.encode())
        os.fsync(fd)
    except OSError as exc:
        logger.debug("could not stamp claim identity: %s", exc)


def read_holder(session_path) -> Holder | None:
    """Who currently claims ``session_path``, or None if unreadable.

    Only meaningful right after a ``try_acquire`` returned None: the file
    outlives the lock, so its contents describe the *last* holder, who may be
    long gone. Never use this to decide ownership — that is what the lock is for.
    """
    try:
        data = json.loads(claim_path(session_path).read_text())
    except (OSError, ValueError):
        return None
    try:
        return Holder(pid=int(data["pid"]), kind=str(data["kind"]), endpoint=data.get("endpoint"))
    except (KeyError, TypeError, ValueError):
        return None
