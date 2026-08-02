"""Import a Claude Code CLI memory store into marim's memory.

Claude Code keeps memory per *project directory*, outside the repo, under
``<claude-config>/projects/<cwd-slug>/memory/``. The on-disk shape there is the
one :mod:`marim_harness.workspace.memory` deliberately mirrors — a ``MEMORY.md``
index of one-line pointers plus one ``<slug>.md`` per fact — so this module is a
format *bridge*, not a translation.

The split follows the house convention: everything above ``read_source`` is
pure (path math, frontmatter parsing, conflict planning) and unit-tested
directly; ``read_source``, ``target_state``, and ``apply_plan`` are the
disk-touching functions, and ``apply_plan`` delegates every write to
``memory.save_memory`` so the memory format keeps exactly one writer.
"""

import os
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

import yaml

from ._frontmatter import FRONTMATTER_RE
from .memory import MemoryScope, index_entries, save_memory

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


@dataclass(frozen=True)
class PlannedImport:
    """What the importer decided to do about one source memory. ``reason`` is
    empty except on a skip, where it explains the conflict well enough for the
    user to decide whether ``--force`` is what they want."""

    action: str  # "import" | "overwrite" | "skip"
    slug: str
    title: str
    reason: str = ""


def target_state(scope: MemoryScope) -> tuple[set[str], dict[str, str]]:
    """The target scope's existing ``<slug>.md`` files and its ``title -> slug``
    map. Slugs come from the *files*, not the index, because the index can be
    stale — the same reasoning as ``memory._link_saved``. Titles necessarily
    come from the index, which is where ``_allocate_slug`` reads them."""
    try:
        slugs = {p.stem for p in scope.root.glob("*.md") if p.name != _INDEX_FILE}
    except OSError:
        slugs = set()
    titles = {title: slug for title, slug in index_entries(scope)}
    return slugs, titles


def _conflict(memory_: ImportedMemory, existing_slugs, existing_titles) -> str:
    """Why importing ``memory_`` would destroy something, or ``""`` if it would
    not. Two independent hazards, both real:

    1. The slug's file already exists — the obvious collision.
    2. The title is already claimed by a *different* slug. ``save_memory`` routes
       through ``_allocate_slug``, which reuses an existing entry's slug on a
       title match, so this would write into the other memory's file even though
       the source slug is free. A same-slug title match is not a conflict; that
       is just a refresh of the same memory.
    """
    if memory_.slug in existing_slugs:
        return "already present — use --force"
    owner = existing_titles.get(memory_.title)
    if owner is not None and owner != memory_.slug:
        return f"title already used by {owner!r} — use --force"
    return ""


def plan_import(
    sources: Sequence[ImportedMemory],
    *,
    existing_slugs: set[str],
    existing_titles: dict[str, str],
    force: bool,
) -> list[PlannedImport]:
    """Decide import / overwrite / skip for each source, in source order.
    Pure: takes the target's state as data so it can be tested without a disk."""
    planned: list[PlannedImport] = []
    for source in sources:
        reason = _conflict(source, existing_slugs, existing_titles)
        if not reason:
            action, reason = "import", ""
        elif force:
            action, reason = "overwrite", ""
        else:
            action = "skip"
        planned.append(
            PlannedImport(action=action, slug=source.slug, title=source.title, reason=reason)
        )
    return planned


@dataclass(frozen=True)
class ImportResult:
    """The outcome of one ``apply_plan``, as three slug tuples. A non-empty
    ``failed`` is what makes the CLI exit non-zero."""

    imported: tuple[str, ...]
    skipped: tuple[str, ...]
    failed: tuple[str, ...]


def apply_plan(
    plan: Sequence[PlannedImport],
    sources: Sequence[ImportedMemory],
    scope: MemoryScope,
) -> ImportResult:
    """Perform every non-skipped entry of ``plan``, writing through
    ``memory.save_memory`` so the memory format keeps a single writer — index
    upsert, slug allocation, atomic writes and the advisory lock all come from
    there rather than being reimplemented here.

    ``save_memory`` never raises (it logs and returns ``None`` on a failed
    write, per its fail-soft contract for tool calls), so a falsy return is the
    only failure signal there is; it is collected rather than swallowed because
    this runs in a CLI, where failures should be loud.
    """
    by_slug = {source.slug: source for source in sources}
    imported: list[str] = []
    skipped: list[str] = []
    failed: list[str] = []
    for entry in plan:
        if entry.action == "skip":
            skipped.append(entry.slug)
            continue
        source = by_slug.get(entry.slug)
        if source is None:  # pragma: no cover - plan is always built from sources
            failed.append(entry.slug)
            continue
        written = save_memory(
            scope,
            name=source.slug,
            description=source.description,
            mem_type=source.mem_type,
            body=source.body,
            title=source.title,
        )
        (imported if written is not None else failed).append(entry.slug)
    return ImportResult(
        imported=tuple(imported), skipped=tuple(skipped), failed=tuple(failed)
    )
