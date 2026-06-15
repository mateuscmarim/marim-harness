from pathlib import Path


class WorkspaceError(Exception):
    """Raised when a path resolves outside the workspace root."""


def resolve_in_workspace(root: Path, path: str) -> Path:
    """Resolve `path` against `root` and ensure it stays inside `root`.

    Raises WorkspaceError if the resolved path escapes the workspace.
    """
    root_resolved = root.resolve()
    candidate = (root_resolved / path).resolve()
    if candidate != root_resolved and root_resolved not in candidate.parents:
        raise WorkspaceError(f"path outside workspace: {path}")
    return candidate
