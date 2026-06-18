import re
from pathlib import Path
from typing import Optional

from pydantic import BaseModel
from pydantic_ai import ModelRetry

from ..workspace.fs import WorkspaceError, resolve_in_workspace

_MAX_GREP_HITS = 200
_MAX_TREE_ENTRIES = 500
# When no explicit ``limit`` is given, a read is capped at this many lines so a
# blind read of a huge file can't flood the context. Pass ``offset``/``limit``
# to page through the rest. An explicit ``limit`` overrides this cap.
_DEFAULT_READ_LIMIT = 2000

# Directories that are almost always noise in a tree view: listed, never expanded.
_TREE_SKIP_DIRS = {
    ".git", "node_modules", "__pycache__", ".venv", ".mypy_cache",
    ".pytest_cache", ".ruff_cache", "dist", "build", ".egg-info",
    ".worktrees",
}


def _safe(root: Path, path: str) -> Path:
    try:
        return resolve_in_workspace(root, path)
    except WorkspaceError as exc:
        raise ModelRetry(str(exc)) from exc


def read_file(
    root: Path, path: str, offset: int = 1, limit: Optional[int] = None
) -> str:
    """Read a text file relative to the workspace root, returning numbered lines.

    ``offset`` is the 1-based line to start at; ``limit`` caps how many lines are
    returned. With no ``limit``, the read is capped at ``_DEFAULT_READ_LIMIT``
    lines so a huge file can't flood the context. When the returned window isn't
    the whole file, a ``[showing lines X-Y of N]`` footer is appended so the
    reader knows to page on with ``offset``/``limit``."""
    if offset < 1:
        raise ModelRetry("offset must be >= 1 (1-based line number).")
    if limit is not None and limit < 1:
        raise ModelRetry("limit must be >= 1.")
    p = _safe(root, path)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    lines = p.read_text(errors="replace").splitlines()
    total = len(lines)
    if total == 0:
        return ""
    if offset > total:
        raise ModelRetry(f"offset {offset} is past end of file ({total} lines).")
    start = offset - 1
    span = limit if limit is not None else _DEFAULT_READ_LIMIT
    end = min(start + span, total)
    body = "\n".join(
        f"{i}\t{line}" for i, line in enumerate(lines[start:end], offset)
    )
    if start == 0 and end == total:
        return body  # whole file — unchanged output
    return f"{body}\n\n[showing lines {offset}-{end} of {total}]"


def write_file(root: Path, path: str, content: str) -> str:
    """Create or overwrite a file relative to the workspace root."""
    p = _safe(root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {path} ({len(content)} bytes)"


class Edit(BaseModel):
    """One find/replace within a file. ``replace_all`` swaps every occurrence;
    otherwise ``old_string`` must match exactly once."""

    old_string: str
    new_string: str
    replace_all: bool = False


def _apply_edit(text: str, edit: Edit, path: str, index: int) -> str:
    """Apply one edit to ``text``, raising ModelRetry (naming the edit) on a bad
    match. ``index`` is 1-based for human-readable messages."""
    count = text.count(edit.old_string)
    if count == 0:
        raise ModelRetry(
            f"edit {index}: old_string not found in {path}. Read the file and copy "
            f"an exact snippet (note earlier edits in this call may have changed it)."
        )
    if count > 1 and not edit.replace_all:
        raise ModelRetry(
            f"edit {index}: old_string found {count} times in {path}. Add surrounding "
            f"context to make it unique, or set replace_all."
        )
    return text.replace(edit.old_string, edit.new_string)


def edit_file(root: Path, path: str, edits: list[Edit]) -> str:
    """Apply a list of edits to one file, in order and all-or-nothing. Each edit
    sees the result of the previous one; the file is written only if all succeed."""
    if not edits:
        raise ModelRetry("no edits given: pass at least one {old_string, new_string}.")
    p = _safe(root, path)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    # Strict decode: unlike read_file (display-only, errors="replace"), edit_file
    # reads-modifies-writes, so a lossy decode would round-trip the undecodable
    # bytes back as U+FFFD and corrupt regions the edit never touched. Refuse
    # instead, with clear feedback.
    try:
        text = p.read_text(encoding="utf-8")
    except UnicodeDecodeError:
        raise ModelRetry(f"can't edit {path}: not a UTF-8 text file.") from None
    for i, edit in enumerate(edits, 1):
        text = _apply_edit(text, edit, path, i)
    p.write_text(text)
    n = len(edits)
    return f"edited {path} ({n} edit{'s' if n != 1 else ''})"


def tree(root: Path, path: str = ".", depth: int = 2) -> str:
    """Render an indented directory tree rooted at ``path``, descending up to
    ``depth`` levels. Dirs sort first (with a trailing slash); known-noise dirs
    are listed but not expanded."""
    base = _safe(root, path)
    if not base.is_dir():
        raise ModelRetry(f"not a directory: {path}")
    lines: list[str] = []
    _walk_tree(base, depth, 0, lines)
    if not lines:
        return "(empty)"
    if len(lines) > _MAX_TREE_ENTRIES:
        lines = lines[:_MAX_TREE_ENTRIES] + ["(truncated)"]
    return "\n".join(lines)


def _walk_tree(directory: Path, depth: int, level: int, lines: list[str]) -> None:
    """Append the entries of ``directory`` to ``lines``, recursing while depth
    allows. Stops early once the entry cap is reached."""
    try:
        entries = list(directory.iterdir())
    except OSError:
        return
    entries.sort(key=lambda p: (p.is_file(), p.name.lower()))
    indent = "  " * level
    for entry in entries:
        if len(lines) > _MAX_TREE_ENTRIES:
            return
        if entry.is_dir():
            lines.append(f"{indent}{entry.name}/")
            if entry.name not in _TREE_SKIP_DIRS and level + 1 < depth:
                _walk_tree(entry, depth, level + 1, lines)
        else:
            lines.append(f"{indent}{entry.name}")


def glob_files(root: Path, pattern: str) -> str:
    """List files under the workspace matching a glob pattern."""
    try:
        candidates = list(root.glob(pattern))
    except (NotImplementedError, ValueError) as exc:
        raise ModelRetry(
            "invalid glob pattern: use a path relative to the workspace, "
            "no leading '/' or '..'"
        ) from exc
    matches = []
    for p in candidates:
        if not p.is_file():
            continue
        if ".worktrees" in p.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
        rel = str(p.relative_to(root))
        try:
            resolve_in_workspace(root, rel)
        except WorkspaceError:
            continue  # skip matches that escape the workspace root
        matches.append(rel)
    matches.sort()
    return "\n".join(matches) if matches else "(no matches)"


def grep(root: Path, pattern: str, path: Optional[str] = None) -> str:
    """Search file contents for a regex, returning `relpath:line:text` hits."""
    rx = re.compile(pattern)
    base = _safe(root, path) if path else root
    files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    out: list[str] = []
    for f in files:
        if ".worktrees" in f.relative_to(root).parts:
            continue  # skip sibling worktree checkouts
        # Skip files that resolve outside the workspace — e.g. an in-tree symlink
        # pointing at /etc/passwd. rglob yields the link; reading it would follow
        # it out of the sandbox, so gate each match the same way read_file does.
        try:
            resolve_in_workspace(root, str(f.relative_to(root)))
        except (WorkspaceError, ValueError):
            continue
        try:
            for i, line in enumerate(f.read_text().splitlines(), 1):
                if rx.search(line):
                    out.append(f"{f.relative_to(root)}:{i}:{line}")
                    if len(out) >= _MAX_GREP_HITS:
                        out.append("(truncated)")
                        return "\n".join(out)
        except (UnicodeDecodeError, OSError):
            continue
    return "\n".join(out) if out else "(no matches)"
