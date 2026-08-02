"""Import a Claude Code CLI memory store into marim's memory.

Claude Code keeps memory per *project directory*, outside the repo, under
``<claude-config>/projects/<cwd-slug>/memory/``. The on-disk shape there is the
one :mod:`marim_harness.workspace.memory` deliberately mirrors — a ``MEMORY.md``
index of one-line pointers plus one ``<slug>.md`` per fact — so this module is a
format *bridge*, not a translation.

The split follows the house convention: everything above ``read_source`` is
pure (path math, frontmatter parsing, conflict planning) and unit-tested
directly; ``read_source`` and ``apply_plan`` are the only functions that touch
disk, and ``apply_plan`` delegates every write to ``memory.save_memory`` so the
memory format keeps exactly one writer.
"""

import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path

import yaml

from ._frontmatter import FRONTMATTER_RE
from .memory import MemoryScope, index_entries

_DEFAULT_CLAUDE_DIRNAME = ".claude"

# Claude's project-dir naming: every path separator and every dot becomes a
# dash. Both characters share one rule, which is why `/home/x/.local` yields the
# doubled `-home-x--local` seen on disk (one dash for the `/`, one for the `.`).
_SLUG_CHARS_RE = re.compile(r"[/.]")

_INDEX_FILE = "MEMORY.md"
_DEFAULT_TYPE = "project"


def claude_config_dir(env: Mapping[str, str] | None = None) -> Path:
    """Claude Code's config root: ``$CLAUDE_CONFIG_DIR`` when set to a non-blank
    value, else ``~/.claude``. ``env`` defaults to the live environment and is
    injectable so the pure path helpers stay testable without monkeypatching."""
    env = os.environ if env is None else env
    raw = (env.get("CLAUDE_CONFIG_DIR") or "").strip()
    if raw:
        return Path(raw).expanduser()
    return Path.home() / _DEFAULT_CLAUDE_DIRNAME


def claude_project_slug(path: Path | str) -> str:
    """The directory name Claude Code uses for ``path``'s project.

    The path is resolved first (absolute, ``..`` collapsed, symlinks followed)
    so a relative or messy workspace argument lands on the same slug the Claude
    CLI would have produced from its own cwd.
    """
    resolved = Path(path).expanduser().resolve()
    return _SLUG_CHARS_RE.sub("-", str(resolved))


def claude_memory_dir(workspace: Path | str, *, config_dir: Path) -> Path:
    """Where Claude Code keeps the memory store for ``workspace``. The directory
    is not guaranteed to exist — callers check and fall back to listing."""
    return Path(config_dir) / "projects" / claude_project_slug(workspace) / "memory"


@dataclass(frozen=True)
class ImportedMemory:
    """One Claude memory file, parsed into exactly the arguments
    ``memory.save_memory`` takes."""

    slug: str
    title: str
    description: str
    mem_type: str
    body: str


@dataclass(frozen=True)
class SourceScan:
    """What one pass over a Claude memory dir found: the memories worth
    importing, plus a human-readable line per file that could not be read or
    parsed. Problems are reported, never fatal — one corrupt file must not cost
    the user the rest of their store."""

    memories: tuple[ImportedMemory, ...]
    problems: tuple[str, ...]


def parse_memory_file(text: str, *, slug: str, title: str) -> ImportedMemory | None:
    """Parse one Claude memory file. Returns ``None`` when the text has no
    parseable YAML mapping frontmatter — marim's format always writes one, so a
    file without it is not a memory (a stray note, a partial write) and is
    skipped rather than imported with empty metadata.

    ``slug`` comes from the filename and ``title`` from the source index; the
    file's own ``name:`` key is deliberately ignored, since the filename is what
    the index links to and ``save_memory`` re-renders ``name:`` regardless.
    Claude's extra keys (``node_type``, ``originSessionId``, ``modified``) are
    dropped: marim reads none of them, and passing them through would fork the
    format the two tools currently share.
    """
    match = FRONTMATTER_RE.match(text)
    if match is None:
        return None
    try:
        data = yaml.safe_load(match.group(1))
    except yaml.YAMLError:
        return None
    if not isinstance(data, dict):
        return None
    description = data.get("description")
    metadata = data.get("metadata")
    mem_type = metadata.get("type") if isinstance(metadata, dict) else None
    return ImportedMemory(
        slug=slug,
        title=title,
        description=str(description).strip() if isinstance(description, str) else "",
        # save_memory's _render_frontmatter coerces an unrecognized type to
        # "project" anyway; defaulting here too keeps the parsed value honest
        # about what will be written.
        mem_type=mem_type if isinstance(mem_type, str) else _DEFAULT_TYPE,
        body=match.group(2),
    )


def read_source(memory_dir: Path) -> SourceScan:
    """Every parseable memory in a Claude memory dir, sorted by slug.

    Titles come from the dir's own ``MEMORY.md``, read through marim's index
    parser; a file with no index entry falls back to its slug as the title.
    """
    titles = {slug: title for title, slug in index_entries(MemoryScope("claude", memory_dir))}
    memories: list[ImportedMemory] = []
    problems: list[str] = []
    for path in sorted(memory_dir.glob("*.md")):
        if path.name == _INDEX_FILE:
            continue
        try:
            text = path.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"{path.name}: unreadable ({exc.__class__.__name__})")
            continue
        parsed = parse_memory_file(text, slug=path.stem, title=titles.get(path.stem, path.stem))
        if parsed is None:
            problems.append(f"{path.name}: no usable frontmatter — skipped")
            continue
        memories.append(parsed)
    return SourceScan(memories=tuple(memories), problems=tuple(problems))
