"""Atomic, durable text writes.

A leaf module (imports only the stdlib) so any subsystem — sessions, checkpoints,
memory — can persist a file without a half-written or clobbered result. The old
pattern, ``path.with_suffix(".json.tmp")`` + ``replace``, was atomic against
*corruption* but used a single deterministic temp name, so two writers racing on
the same target (a flush + a persist, or a headless run + the TUI on one session)
clobbered each other's temp file. It also never fsynced, so a crash could leave
the renamed file's data unwritten. ``atomic_write_text`` fixes both: a unique
temp per write, fsynced before the swap, with the directory fsynced best-effort.
"""

import os
import tempfile
from pathlib import Path


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically and durably.

    Writes to a uniquely named temp file in the same directory (so concurrent
    writers never collide on a shared name), flushes and fsyncs it, then
    ``os.replace``s it over the target — an atomic swap a crash can't leave
    half-applied. The parent directory is fsynced best-effort so the rename
    itself survives power loss. The target's directory must already exist.
    """
    path = Path(path)
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding=encoding) as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # don't leave a temp behind on failure
        raise
    _fsync_dir(directory)


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and durably — the bytes counterpart
    of :func:`atomic_write_text`. Same unique-temp-then-``os.replace`` strategy, so
    two writers racing on the same target never collide on a shared temp name (the
    bug in the old ``out.with_suffix(".tmp")`` pattern). The target's directory must
    already exist."""
    path = Path(path)
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # don't leave a temp behind on failure
        raise
    _fsync_dir(directory)


def _fsync_dir(directory: Path) -> None:
    """fsync a directory so a rename into it is durable. Best-effort: some
    platforms/filesystems can't open a directory for fsync, in which case the
    rename is still atomic, just not guaranteed durable across power loss."""
    try:
        dir_fd = os.open(directory, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(dir_fd)
    except OSError:
        pass
    finally:
        os.close(dir_fd)
