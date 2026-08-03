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

import hashlib
import logging
import re
import unicodedata
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

from ..atomic_io import atomic_write_text, file_lock
from ..config import config_dir

logger = logging.getLogger(__name__)

_INDEX_FILE = "MEMORY.md"
_VALID_TYPES = ("user", "feedback", "project", "reference")

# Matches an index entry by its OWN link target — the first `](…md)` of an index
# line — never by bare substring. A plain ``"](slug.md)" in raw`` test would
# also fire on a *different* entry whose hook text happens to mention
# ``slug.md`` (e.g. "see [link](auth.md)"), hitting the wrong line. Shared by
# the upsert (refresh-in-place) and delete (drop-the-line) paths so the two
# can't disagree about what "this entry's line" means. The title is captured too
# so save-time slug allocation can tell whose entry a slug belongs to; titles are
# sanitized on write (index_title) so the FIRST `](…md)` is always the real link.
_ENTRY_LINK_RE = re.compile(r"^- \[(?P<title>[^\]]*)\]\((?P<slug>[^)]+)\.md\)")

# ``[[name]]`` wikilinks inside a memory body. Names may be titles or slugs
# (annotate_links slugifies either way); brackets inside brackets are not
# supported — the convention is flat links, mirroring Claude Code's memory.
_WIKILINK_RE = re.compile(r"\[\[([^\[\]]+?)\]\]")


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
    """Reduce a title to a filesystem-safe ASCII slug, falling back to a
    per-title hash. Accents are transliterated (``usuário`` -> ``usuario``) so
    accented and unaccented spellings collapse to the same slug.

    The fallback is ``memory-<hash>``, NOT a bare constant: a title with no ASCII
    letters or digits at all (all-CJK/emoji) slugifies to empty, so a shared
    ``"memory"`` constant would map EVERY such title to one file — a second
    non-ASCII memory silently overwrites the first and replaces its index line.
    The hash is derived from the title and is deterministic, so read_memory /
    delete_memory (which re-slugify the same name) still resolve to the file."""
    decomposed = unicodedata.normalize("NFKD", name or "")
    ascii_only = decomposed.encode("ascii", "ignore").decode("ascii")
    slug = re.sub(r"[^a-z0-9]+", "-", ascii_only.lower()).strip("-")
    if slug:
        return slug
    return f"memory-{hashlib.sha256((name or '').encode('utf-8')).hexdigest()[:8]}"


def index_title(title: str) -> str:
    """Sanitize a title for the one-line index entry: collapse to a single line
    and strip markdown link punctuation ``[]()``.

    A title containing ``](`` — e.g. a pasted ``see [x](y.md)`` — would forge a
    second ``](slug.md)`` link on the entry line, so ``_ENTRY_LINK_RE`` (which
    anchors on the FIRST such link) captures the wrong slug and the upsert/delete
    dedup misfires, silently accumulating duplicate lines for the same memory.
    Removing the brackets leaves the entry's own link as the only one present.

    Public for the same reason ``index_entries`` is: the Claude importer has to
    predict, before writing, which index entry a save will land on. This is the
    exact normalization ``_upsert_index_line`` applies on the way to disk and
    ``allocate_slug`` compares against, so a caller that wants to know "will
    these two titles be the same entry?" must ask *this* function rather than
    compare raw strings — that mismatch is precisely what let an import clobber
    a marim-authored memory."""
    return _single_line(re.sub(r"[\[\]()]", "", title or ""))


def index_entries(scope: MemoryScope) -> list[tuple[str, str]]:
    """``(title, slug)`` for every entry in ``MEMORY.md``, in file order.
    Best-effort: an absent/unreadable index yields ``[]`` (never raises).

    Public because the Claude importer (``workspace.claude_import``) reads a
    *foreign* store's index through the same parser — Claude keeps a memory's
    title only in its index line, and sharing this one regex is what keeps the
    two readers from drifting."""
    path = scope.root / _INDEX_FILE
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except (OSError, UnicodeDecodeError):
        return []
    entries: list[tuple[str, str]] = []
    for raw in lines:
        m = _ENTRY_LINK_RE.match(raw)
        if m:
            entries.append((m.group("title").strip(), m.group("slug")))
    return entries


@dataclass(frozen=True)
class SlugAllocation:
    """Where a save will land. ``slug`` is the ``<slug>.md`` that will be written;
    ``base`` is the slug the name alone would have produced; ``title_owner`` is
    the existing index entry whose title claimed ``slug``, or ``None`` when the
    slug came from the name.

    ``slug != base`` is the interesting case — the write is being redirected onto
    a file the caller did not name, either because an entry already holds this
    title (``title_owner`` set) or because the base was taken by a different
    title (suffixed ``base-2``). A caller deciding whether a save would destroy
    something must look at ``slug``, never at ``base``."""

    slug: str
    base: str
    title_owner: str | None


def allocate_slug(
    entries: Sequence[tuple[str, str]], *, name: str, title: str
) -> SlugAllocation:
    """The slug ``title`` should be saved under, disambiguating collisions,
    decided against ``entries`` (``(title, slug)`` index lines in file order).

    Re-saving an existing title reuses that entry's slug (so the write updates in
    place). Otherwise the base is ``_slugify(name)``; if the base is already
    claimed by a DIFFERENT title's entry — two distinct titles that slugify alike,
    e.g. ``"Foo Bar"`` vs ``"foo bar"`` — step to ``base-2``, ``base-3``, … so the
    newcomer neither overwrites the incumbent's ``<slug>.md`` nor replaces its
    index line. The first writer keeps the clean base slug; the collision loser is
    reachable by the suffixed slug shown in the index (``recall`` takes a slug).
    (Residual: read_memory/delete_memory re-slugify a bare title to the base, so a
    loser is not reachable by its title — only by its index slug.)

    Public, and pure over ``entries``, because the Claude importer must decide
    *before* writing whether an import would destroy an existing memory. That
    question is only answerable by running this allocation: any guard that
    re-derives the answer from raw slugs and raw titles is comparing values this
    function normalizes (``_slugify`` / ``index_title``), so it is strictly
    weaker than the write it guards — which is exactly how an import came to
    silently overwrite a marim-authored memory. Sharing the allocator, not just
    the normalizers, is what makes that class of drift unrepresentable."""
    base = _slugify(name)
    wanted = index_title(title)
    for etitle, eslug in entries:
        if etitle == wanted:
            return SlugAllocation(slug=eslug, base=base, title_owner=eslug)
    taken = {eslug for _, eslug in entries}
    if base not in taken:
        return SlugAllocation(slug=base, base=base, title_owner=None)
    n = 2
    while f"{base}-{n}" in taken:
        n += 1
    return SlugAllocation(slug=f"{base}-{n}", base=base, title_owner=None)


def _allocate_slug(scope: MemoryScope, *, name: str, title: str) -> str:
    """``allocate_slug`` against the scope's live index — the disk-reading half,
    kept separate so the decision itself stays pure and testable as data."""
    return allocate_slug(index_entries(scope), name=name, title=title).slug


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
    # Sanitize the title into the link so a ``](`` in it can't forge a second
    # link and defeat the slug-keyed dedup below (see index_title).
    line = f"- [{index_title(title)}]({slug}.md) — {hook}"

    # Serialize the read+modify+write of the shared index with a best-effort
    # advisory lock: two concurrent save_memory calls each read the old index,
    # add their own line, and write — last writer wins, silently dropping the
    # other's entry. The lock makes each upsert see the prior one's result.
    with file_lock(path):
        existing = path.read_text(encoding="utf-8").splitlines() if path.exists() else []
        new_lines, replaced = [], False
        for raw in existing:
            m = _ENTRY_LINK_RE.match(raw)
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
    # Allocate a collision-safe slug (see _allocate_slug) rather than the bare
    # _slugify: two distinct titles that slugify alike must not clobber one
    # another's file and index line. Re-saving the same title reuses its slug.
    slug = _allocate_slug(scope, name=name, title=title)
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
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError: _upsert_index_line's read_text can hit a
        # human-corrupted non-UTF-8 MEMORY.md; load_index/read_memory already
        # treat that as fail-soft, so save_memory must match rather than
        # raise into the turn.
        logger.debug("failed to save memory %s (%s): %s", path, scope.name, exc)
        return None
    logger.debug("saved memory %s (%s)", path, scope.name)
    return path


def _remove_index_line(scope: MemoryScope, *, slug: str) -> None:
    """Drop ``slug``'s pointer from ``MEMORY.md``, preserving every other line.
    Same advisory-lock + atomic-write discipline as ``_upsert_index_line`` so a
    concurrent save can't resurrect the deleted entry's line or lose its own."""
    path = scope.root / _INDEX_FILE
    with file_lock(path):
        if not path.exists():
            return
        kept = []
        for raw in path.read_text(encoding="utf-8").splitlines():
            m = _ENTRY_LINK_RE.match(raw)
            if m and m.group("slug") == slug:
                continue
            kept.append(raw)
        text = "\n".join(kept)
        atomic_write_text(path, text + "\n" if text else "")


def delete_memory(scope: MemoryScope, name: str) -> bool:
    """Delete the memory named ``name`` (title or slug — both slugify to the
    stored filename) and drop its index line. Returns True when the file
    existed and was removed, False when there was nothing to delete or the
    delete failed. Per the module docstring, nothing raises into a turn: OSErrors
    are logged and folded into False, matching save_memory's fail-soft style."""
    slug = _slugify(name)
    path = scope.root / f"{slug}.md"
    try:
        if not path.is_file():
            return False
        path.unlink()
        _remove_index_line(scope, slug=slug)
    except (OSError, UnicodeDecodeError) as exc:
        # UnicodeDecodeError: _remove_index_line's read_text can hit a
        # human-corrupted non-UTF-8 MEMORY.md; load_index/read_memory already
        # treat that as fail-soft, so delete_memory must match rather than
        # raise into the turn.
        logger.debug("failed to delete memory %s (%s): %s", path, scope.name, exc)
        return False
    logger.debug("deleted memory %s (%s)", path, scope.name)
    return True


def extract_links(body: str) -> list[str]:
    """The distinct ``[[name]]`` wikilink targets in a memory body, in first-
    appearance order (whitespace-trimmed; empty links skipped)."""
    seen: dict[str, None] = {}
    for m in _WIKILINK_RE.finditer(body or ""):
        target = m.group(1).strip()
        if target:
            seen.setdefault(target, None)
    return list(seen)


def _link_saved(scope: MemoryScope, target: str) -> bool:
    """Whether ``target`` (a wikilink name) has a saved ``<slug>.md`` file.
    ``Path.is_file()`` re-raises OSErrors that aren't "doesn't exist" (ENOTDIR,
    EBADF, ELOOP, and notably ENAMETOOLONG — a slug over NAME_MAX, e.g. from a
    very long ``[[...]]`` link, raises on Python 3.10/3.12). Per the module
    contract nothing here raises into a turn, so an unresolvable check is just
    treated as "not saved" rather than propagating."""
    try:
        return (scope.root / f"{_slugify(target)}.md").is_file()
    except OSError:
        return False


def annotate_links(scope: MemoryScope, body: str) -> str:
    """Return ``body``, plus — when it contains ``[[name]]`` links — a one-line
    footer telling the model which linked memories are saved and which are still
    unwritten. Existence is checked by slugifying each link and testing for its
    ``<slug>.md`` file (not the index, which could be stale). A dangling link is
    not an error: per the convention it marks a fact worth writing later."""
    links = extract_links(body)
    if not links:
        return body
    saved = [t for t in links if _link_saved(scope, t)]
    unwritten = [t for t in links if t not in saved]
    parts = []
    if saved:
        parts.append("saved: " + ", ".join(saved))
    if unwritten:
        parts.append("not yet written: " + ", ".join(unwritten))
    footer = "\n\nLinked memories — " + "; ".join(parts) + ". Read saved ones with recall."
    return body.rstrip("\n") + footer
