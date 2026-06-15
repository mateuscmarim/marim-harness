import re
from pathlib import Path
from typing import Optional

from pydantic_ai import ModelRetry

from ..workspace import WorkspaceError, resolve_in_workspace

_MAX_GREP_HITS = 200


def _safe(root: Path, path: str) -> Path:
    try:
        return resolve_in_workspace(root, path)
    except WorkspaceError as exc:
        raise ModelRetry(str(exc)) from exc


def read_file(root: Path, path: str) -> str:
    """Read a text file relative to the workspace root, returning numbered lines."""
    p = _safe(root, path)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    lines = p.read_text(errors="replace").splitlines()
    return "\n".join(f"{i}\t{line}" for i, line in enumerate(lines, 1))


def write_file(root: Path, path: str, content: str) -> str:
    """Create or overwrite a file relative to the workspace root."""
    p = _safe(root, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content)
    return f"wrote {path} ({len(content)} bytes)"


def edit_file(root: Path, path: str, old_string: str, new_string: str) -> str:
    """Replace the unique occurrence of old_string with new_string."""
    p = _safe(root, path)
    if not p.is_file():
        raise ModelRetry(f"not a file: {path}")
    text = p.read_text()
    count = text.count(old_string)
    if count == 0:
        raise ModelRetry(
            f"old_string not found in {path}. Read the file and copy an exact, unique snippet."
        )
    if count > 1:
        raise ModelRetry(
            f"old_string found {count} times in {path}. Add surrounding context to make it unique."
        )
    p.write_text(text.replace(old_string, new_string))
    return f"edited {path}"


def glob_files(root: Path, pattern: str) -> str:
    """List files under the workspace matching a glob pattern."""
    matches = sorted(
        str(p.relative_to(root)) for p in root.glob(pattern) if p.is_file()
    )
    return "\n".join(matches) if matches else "(no matches)"


def grep(root: Path, pattern: str, path: Optional[str] = None) -> str:
    """Search file contents for a regex, returning `relpath:line:text` hits."""
    rx = re.compile(pattern)
    base = _safe(root, path) if path else root
    files = [base] if base.is_file() else [p for p in base.rglob("*") if p.is_file()]
    out: list[str] = []
    for f in files:
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
