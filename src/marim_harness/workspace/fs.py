from pathlib import Path


class WorkspaceError(Exception):
    """Raised when a path resolves outside the workspace root."""


class ReadLedger:
    """Tracks which files have been read this session and their content
    fingerprint at read time, so an editor can refuse to modify a file the agent
    hasn't seen — or has seen but that has since changed on disk (a linter, a
    concurrent process, or the user editing it). That guard is what stops an edit
    from being applied against a stale view of the file.

    Keyed by *resolved* absolute path (callers pass the same resolved Path the
    file tools sandbox to, so a symlink and its target collapse to one key). The
    fingerprint is ``(mtime_ns, size)``: cheap to take, no full re-read, and any
    in-place rewrite changes at least one of the two. Stays framework-free
    (returns a reason code rather than raising a tool error) so the boundary
    matches the rest of ``workspace.fs``; the tool layer maps the code to a
    model-facing ``ModelRetry``."""

    def __init__(self) -> None:
        self._seen: dict[Path, tuple[int, int]] = {}

    def record(self, p: Path) -> None:
        """Fingerprint ``p`` as freshly seen — after a successful read, and after
        a write/edit so a follow-up modification in the same turn isn't taken for
        a stale one. Silently ignores a vanished/unreadable path (nothing to
        record; the next edit attempt reports the real error)."""
        try:
            st = p.stat()
        except OSError:
            return
        self._seen[p] = (st.st_mtime_ns, st.st_size)

    def staleness(self, p: Path) -> str | None:
        """``None`` when ``p`` is safe to edit; otherwise a reason code:
        ``"unread"`` (never read this session) or ``"changed"`` (read, but the
        on-disk fingerprint no longer matches). A path that can't be stat'd is
        treated as not-stale so the normal edit path surfaces the real error."""
        fingerprint = self._seen.get(p)
        if fingerprint is None:
            return "unread"
        try:
            st = p.stat()
        except OSError:
            return None
        if (st.st_mtime_ns, st.st_size) != fingerprint:
            return "changed"
        return None


def resolve_in_workspace(root: Path, path: str) -> Path:
    """Resolve `path` against `root` and ensure it stays inside `root`.

    Raises WorkspaceError if the resolved path escapes the workspace.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / path).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise WorkspaceError(f"path outside workspace: {path}")
    return candidate
