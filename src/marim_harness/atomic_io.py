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

import contextlib
import glob
import os
import tempfile
from collections.abc import Iterator
from pathlib import Path

# Advisory file locking is POSIX-only via fcntl. Import it guarded so this leaf
# module still loads on Windows (or any platform without fcntl) — the lock then
# degrades to a no-op rather than breaking the whole harness on import.
try:  # pragma: no cover - exercised by import on the running platform
    import fcntl
except ImportError:  # pragma: no cover - Windows / no-fcntl platforms
    fcntl = None  # type: ignore[assignment]


@contextlib.contextmanager
def file_lock(path: Path) -> Iterator[None]:
    """Hold a *best-effort advisory* exclusive lock around a critical section.

    This serializes a read-modify-write across processes (e.g. the TUI and a
    headless run both saving the same session, or two ``save_memory`` calls
    racing on the shared ``MEMORY.md`` index) so the last writer can't silently
    clobber the other's work. It is **advisory**: it only excludes other callers
    that also take this lock — it does not stop an unrelated process from writing
    the target file directly.

    The lock is taken on a sidecar ``<path>.lock`` file (never on the target
    itself, so the lock outlives the ``os.replace`` swaps the data file goes
    through). On POSIX it uses ``fcntl.flock`` (released automatically when the
    fd closes, even if the process dies). When ``fcntl`` is unavailable (Windows
    or any non-POSIX platform), this degrades to a no-op so callers stay portable
    — locking is a best-effort safety net, not a correctness guarantee. Any error
    creating or locking the file is swallowed: a lock we can't take must never
    break the write it was only meant to protect.
    """
    path = Path(path)
    lock_path = path.with_name(f"{path.name}.lock")
    if fcntl is None:
        # No advisory locking available — run the body unprotected rather than
        # fail. Best-effort means best-effort.
        yield
        return
    fd = None
    try:
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o644)
        fcntl.flock(fd, fcntl.LOCK_EX)
    except OSError:
        # Couldn't create or acquire the lock — proceed unprotected so the
        # caller's write still happens; locking is only a safety net.
        if fd is not None:
            os.close(fd)
            fd = None
        yield
        return
    try:
        yield
    finally:
        # Releasing is implicit on close, but unlock explicitly first so the
        # intent is clear and the window without the lock is as short as possible.
        with contextlib.suppress(OSError):
            fcntl.flock(fd, fcntl.LOCK_UN)
        with contextlib.suppress(OSError):
            os.close(fd)


def _sweep_stale_temps(directory: Path, name: str) -> None:
    """Best-effort removal of leftover ``.<name>.*.tmp`` files in ``directory``.

    A crash between ``mkstemp`` and ``os.replace`` leaves a uniquely named temp
    behind (the swap that would have consumed it never happened). These are inert
    but accumulate; sweep them opportunistically on the next write to the same
    target. Errors are ignored — a sweep that fails must never break the write."""
    pattern = os.path.join(glob.escape(str(directory)), f".{name}.*.tmp")
    for stale in glob.glob(pattern):
        with contextlib.suppress(OSError):
            os.unlink(stale)


def _atomic_write_core(path: Path, open_kwargs: dict, write_fn) -> None:
    """Shared temp-file lifecycle for atomic writes: mkstemp → write → fsync → replace."""
    directory = path.parent
    fd, tmp_name = tempfile.mkstemp(dir=directory, prefix=f".{path.name}.", suffix=".tmp")
    tmp = Path(tmp_name)
    try:
        with os.fdopen(fd, **open_kwargs) as f:
            write_fn(f)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)  # don't leave a temp behind on failure
        raise
    _fsync_dir(directory)
    _sweep_stale_temps(directory, path.name)


def atomic_write_text(path: Path, text: str, *, encoding: str = "utf-8") -> None:
    """Write ``text`` to ``path`` atomically and durably.

    Writes to a uniquely named temp file in the same directory (so concurrent
    writers never collide on a shared name), flushes and fsyncs it, then
    ``os.replace``s it over the target — an atomic swap a crash can't leave
    half-applied. The parent directory is fsynced best-effort so the rename
    itself survives power loss. The target's directory must already exist.
    """
    _atomic_write_core(Path(path), {"mode": "w", "encoding": encoding}, lambda f: f.write(text))


def atomic_write_bytes(path: Path, data: bytes) -> None:
    """Write ``data`` to ``path`` atomically and durably — the bytes counterpart
    of :func:`atomic_write_text`. Same unique-temp-then-``os.replace`` strategy, so
    two writers racing on the same target never collide on a shared temp name (the
    bug in the old ``out.with_suffix(".tmp")`` pattern). The target's directory must
    already exist."""
    _atomic_write_core(Path(path), {"mode": "wb"}, lambda f: f.write(data))


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
