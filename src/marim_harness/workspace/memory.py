"""Native markdown memory, mirroring Claude Code's design.

Memory lives in two scopes — global (per-user, across every workspace) and
project (committed alongside a repo) — both with the same shape: a small
``MEMORY.md`` index (one line per fact) plus one ``<slug>.md`` file per fact
carrying YAML frontmatter and a markdown body. The index is injected into the
system prompt each turn (it's tiny); full bodies are pulled in on demand with
the ordinary ``read_file`` tool. ``save_memory`` is the single writer, shared by
the ``remember`` tool and the ``/remember`` command, so the file format lives in
one place. Nothing here ever raises into a turn — dirs are created on demand.
"""

import logging
import re
import unicodedata
from dataclasses import dataclass
from pathlib import Path

from ..atomic_io import atomic_write_text, file_lock
from ..config import config_dir

logger = logging.getLogger(__name__)

_INDEX_FILE = "MEMORY.md"
_VALID_TYPES = ("user", "feedback", "project", "reference")


@dataclass(frozen=True)
class MemoryScope:
    """One memory store: a name and the directory holding its index and files."""

    name: str
    root: Path


def global_scope() -> MemoryScope:
    """Per-user memory, under the marim config dir (respects XDG_CONFIG_HOME)."""
    return MemoryScope("global", config_dir() / "memory")


def project_scope(workspace_root) -> MemoryScope:
    """Repo-local memory, under ``<workspace>/.marim/memory``."""
    return MemoryScope("project", Path(workspace_root) / ".marim" / "memory")


def _single_line(text: str) -> str:
    """Collapse a model-controlled value to a single line (all whitespace runs,
    including newlines, become one space; ends trimmed). ``description`` and
    ``title`` are written into the YAML frontmatter and the always-injected
    MEMORY.md index; a raw newline there injects a spurious frontmatter key or an
    orphan index line — the latter silently defeats the upsert dedup and
    accumulates in the index. The body is exempt (multi-line markdown is fine)."""
    return " ".join((text or "").split())


def _slugify(name: str) -> str:
    """Reduce a title to a filesystem-safe ASCII slug, falling back to ``memory``.
    Accents are transliterated (``usuário`` -> ``usuario``) so accented and
    unaccented spellings collapse to the same slug."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    return slug or "memory"


def load_index(scope: MemoryScope) -> str | None:
    """Return the scope's ``MEMORY.md`` text (stripped), or ``None`` if absent,
    empty, or unreadable — a broken index must never break a turn."""
    path = scope.root / _INDEX_FILE
    try:
        text = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("failed to load memory index %s: %s", path, exc)
        return None
    return text or None


def read_memory(scope: MemoryScope, name: str) -> str:
    """Return the full text of a memory file by name (its title or slug; both
    slugify to the same file). Memory files live in marim's own dirs — global is
    outside the workspace — so this reads them directly rather than through the
    workspace-sandboxed read_file tool. ``name`` may be the entry's title or its
    slug — both slugify to the stored filename, which is always slug-named — so a
    free-form name resolves either way. Returns a notice if no file matches."""
    slug = _slugify(name)
    path = scope.root / f"{slug}.md"
    try:
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        logger.debug("failed to read memory %s: %s", path, exc)
        return f"No {scope.name} memory named {slug!r}."


def _render_frontmatter(*, slug: str, description: str, mem_type: str) -> str:
    mem_type = mem_type if mem_type in _VALID_TYPES else "project"
    return (
        "---\n"
        f"name: {slug}\n"
        f"description: {description}\n"
        "metadata:\n"
        f"  type: {mem_type}\n"
        "---\n"
    )


def _upsert_index_line(scope: MemoryScope, *, slug: str, title: str, hook: str) -> None:
    """Add or refresh the one-line pointer for ``slug`` in ``MEMORY.md``,
    preserving every other line and never duplicating an entry."""
    path = scope.root / _INDEX_FILE
    line = f"- [{title}]({slug}.md) — {hook}"
    # Match this entry by its OWN link target — the first `](…md)` of an index
    # line — not by a bare substring. A plain ``"](slug.md)" in raw`` test would
    # also fire on a *different* entry whose hook text happens to mention
    # ``slug.md`` (e.g. "see [link](auth.md)"), clobbering the wrong line.
    entry_link = re.compile(r"^- \[.*?\]\((?P<slug>[^)]+)\.md\)")

    # Serialize the read+modify+write of the shared index with a best-effort
    # advisory lock: two concurrent save_memory calls each read the old index,
    # add their own line, and write — last writer wins, silently dropping the
    # other's entry. The lock makes each upsert see the prior one's result.
    with file_lock(path):
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        new_lines, replaced = [], False
        for raw in existing:
            m = entry_link.match(raw)
            if m and m.group("slug") == slug:
                new_lines.append(line)
                replaced = True
            else:
                new_lines.append(raw)
        if not replaced:
            new_lines.append(line)

        atomic_write_text(path, "\n".join(new_lines) + "\n")


def save_memory(
    scope: MemoryScope,
    *,
    name: str,
    description: str,
    mem_type: str,
    body: str,
    title: str,
) -> Path | None:
    """Write ``<slug>.md`` (frontmatter + body) and upsert its index line.
    Returns the path to the memory file, or ``None`` if the write failed (e.g. an
    unwritable/read-only memory directory). Creates the scope dir on demand.

    Per this module's docstring, nothing here raises into a turn: every write —
    the scope dir, the memory file, and the index upsert — is wrapped in one
    try/except OSError, matching load_index/read_memory's existing fail-soft
    style (log and return a caller-checkable "didn't work" value instead of
    propagating). The caller (the ``remember`` tool) is expected to turn a
    ``None`` into an actionable message rather than crash the model's turn."""
    slug = _slugify(name)
    # Clamp the single-line fields before they reach the frontmatter / index; the
    # body keeps its newlines.
    description = _single_line(description)
    title = _single_line(title)
    frontmatter = _render_frontmatter(slug=slug, description=description, mem_type=mem_type)
    path = scope.root / f"{slug}.md"
    try:
        scope.root.mkdir(parents=True, exist_ok=True)
        atomic_write_text(path, f"{frontmatter}\n{body.strip()}\n")
        _upsert_index_line(scope, slug=slug, title=title, hook=description)
    except OSError as exc:
        logger.debug("failed to save memory %s (%s): %s", path, scope.name, exc)
        return None
    logger.debug("saved memory %s (%s)", path, scope.name)
    return path
