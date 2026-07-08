from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

if TYPE_CHECKING:
    from ..mcp import McpManager

from ..config import config_dir
from ..mcp.catalog import tool_catalog_text
from ..plugins import plugin_instruction_texts
from ..workspace import (
    agents_index_text,
    discover_agents,
    discover_skills,
    global_scope,
    load_index,
    project_scope,
    skills_index_text,
)
from .deps import Deps, HarnessAgent

_PROJECT_INSTRUCTIONS_FILE = "AGENTS.md"
_PROJECT_FALLBACK_FILES = ("AGENTS.md", "CLAUDE.md")
# The filename memory/ writes its one-line index under (memory._INDEX_FILE). We
# only need it here to stat the backing file for the cache fingerprint; the read
# itself still goes through load_index. The memory-index invalidation test guards
# against this drifting from memory's constant.
_MEMORY_INDEX_FILE = "MEMORY.md"


# --- mtime-keyed read cache -------------------------------------------------
#
# pydantic-ai rebuilds every ``@agent.instructions`` closure on *each* model
# request, not once per turn. Without memoization, the closures below re-read
# AGENTS.md / CLAUDE.md / MEMORY.md from disk on every request even though their
# content is stable within (and usually across) turns. We key each memoized
# value on a cheap stat fingerprint — ``(mtime_ns, size)`` of the backing
# file(s) — so a cached value is reused while the files are unchanged but is
# transparently recomputed the moment one is edited, added, or removed. This is
# *not* a blind once-only cache: editing AGENTS.md mid-session is picked up on
# the next request because the fingerprint changes. Mirrors the stat-fingerprint
# discovery cache in ``workspace/_discovery.py``.

_StatKey = tuple[int, int] | None


def _stat_key(path: Path) -> _StatKey:
    """A cheap ``(mtime_ns, size)`` fingerprint of one file, or None if it can't
    be stat'd (absent/unreadable). Size is included so a same-mtime edit that
    changes length still invalidates."""
    try:
        st = path.stat()
    except OSError:
        return None
    return (st.st_mtime_ns, st.st_size)


def _cached_by_stat(cache: dict, key, paths: list[Path], compute):
    """Return ``compute()`` memoized in ``cache[key]`` under a stat fingerprint
    of ``paths``; recompute only when that fingerprint changes. Gating on the
    cache *entry* (not its value) means a ``None``/empty result is cached too."""
    sig = tuple(_stat_key(p) for p in paths)
    hit = cache.get(key)
    if hit is not None and hit[0] == sig:
        return hit[1]
    value = compute()
    cache[key] = (sig, value)
    return value


# Keyed by (resolved workspace root str, filename-or-None); see _cached_by_stat.
_PROJECT_INSTRUCTIONS_CACHE: dict = {}
# Keyed by resolved workspace root str (the per-turn memory index block).
_MEMORY_INDEX_CACHE: dict = {}

_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON — save durable user preferences, feedback, and "
    "project conventions with remember. Skip recoverable info, one-off details, "
    "and secrets. Update existing entries over adding duplicates."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks (e.g. 'remember that…' "
    "or /remember). Do not save proactively."
)


def load_project_instructions(
    workspace_root, filename: str | None = None
) -> str | None:
    """Read project-specific agent instructions from the workspace root.

    When *filename* is given, try only that file.  Otherwise iterate the
    fallback list (``AGENTS.md``, ``CLAUDE.md``) and return the first
    non-empty result.  Returns ``None`` if no file is found or all are
    empty/unreadable — a broken file must never break a turn.

    The read is memoized under a stat fingerprint of all candidate paths, so a
    closure that calls this on every model request re-reads only when one of the
    candidate files actually changes on disk (see _cached_by_stat).
    """
    candidates = [filename] if filename is not None else list(_PROJECT_FALLBACK_FILES)
    paths = [Path(workspace_root) / name for name in candidates]
    key = (str(Path(workspace_root).resolve()), filename)
    return _cached_by_stat(
        _PROJECT_INSTRUCTIONS_CACHE, key, paths, lambda: _read_first_nonempty(paths)
    )


def _read_first_nonempty(paths: list[Path]) -> str | None:
    """First non-empty stripped text across ``paths``, or None. A broken/absent
    file is skipped, never fatal."""
    for path in paths:
        try:
            text = path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            continue
        if text:
            return text
    return None


def global_instructions_path() -> Path:
    """The user-level agent instructions file, a sibling of the global config
    (``~/.config/marim/AGENTS.md``)."""
    return config_dir() / _PROJECT_INSTRUCTIONS_FILE


def load_global_instructions() -> str | None:
    """Read user-level standing instructions from ``~/.config/marim/AGENTS.md``.
    These apply across every project (unlike the per-project ``AGENTS.md``).
    Same fail-safe semantics as :func:`load_project_instructions`."""
    return load_project_instructions(config_dir())


def _memory_index_block(workspace_root) -> str:
    """The injected memory-index block (global then project), or "" if neither
    has a ``MEMORY.md``.

    Memoized under a stat fingerprint of the two ``MEMORY.md`` files so the
    per-request ``_memory_indexes`` closure re-reads them only when one changes.
    load_index still performs the actual read on a miss; we stat the files here
    purely to key the cache."""
    global_scope_ = global_scope()
    project_scope_ = project_scope(workspace_root)
    paths = [
        global_scope_.root / _MEMORY_INDEX_FILE,
        project_scope_.root / _MEMORY_INDEX_FILE,
    ]
    key = str(Path(workspace_root).resolve())
    return _cached_by_stat(
        _MEMORY_INDEX_CACHE,
        key,
        paths,
        lambda: _build_memory_index(global_scope_, project_scope_),
    )


def _build_memory_index(global_scope_, project_scope_) -> str:
    parts = []
    global_index = load_index(global_scope_)
    if global_index:
        parts.append(f"# User memory (global)\n\n{global_index}")
    project_index = load_index(project_scope_)
    if project_index:
        parts.append(f"# Project memory\n\n{project_index}")
    if not parts:
        return ""
    return (
        "Memory index (use recall for full entries, "
        "remember to save):\n\n" + "\n\n".join(parts)
    )


def register_instructions(
    agent: HarnessAgent, mcp_manager: McpManager, proactive_memory: bool
) -> None:
    """Register all dynamic instruction closures on ``agent``."""

    @agent.instructions
    def _global_instructions(ctx: RunContext[Deps]) -> str:
        text = load_global_instructions()
        if not text:
            return ""
        path = global_instructions_path()
        home = Path.home()
        shown = f"~/{path.relative_to(home)}" if path.is_relative_to(home) else str(path)
        return (
            f"Global instructions from {shown} "
            f"(apply to every project):\n\n{text}"
        )

    @agent.instructions
    def _project_instructions(ctx: RunContext[Deps]) -> str:
        text = load_project_instructions(ctx.deps.workspace.root)
        if not text:
            return ""
        return f"Project-specific instructions:\n\n{text}"

    @agent.instructions
    def _plugin_instructions(ctx: RunContext[Deps]) -> str:
        texts = plugin_instruction_texts(ctx.deps.workspace.root)
        if not texts:
            return ""
        blocks = [f"## From plugin '{name}'\n\n{text}" for name, text in texts]
        return (
            "Instructions contributed by installed plugins (treat like "
            "project instructions):\n\n" + "\n\n".join(blocks)
        )

    @agent.instructions
    def _memory_indexes(ctx: RunContext[Deps]) -> str:
        return _memory_index_block(ctx.deps.workspace.root)

    @agent.instructions
    def _skill_index(ctx: RunContext[Deps]) -> str:
        skills = discover_skills(ctx.deps.workspace.root, dirs=ctx.deps.workspace.skill_dirs)
        text = skills_index_text(skills)
        if not text:
            return ""
        return (
            "Available skills below — each is a packaged workflow. When a "
            "task matches one's description, load its full instructions with "
            "the activate_skill tool (by name) and follow them.\n\n" + text
        )

    @agent.instructions
    def _agent_index(ctx: RunContext[Deps]) -> str:
        text = agents_index_text(discover_agents(ctx.deps.workspace.root))
        if not text:
            return ""
        # The mode-reach rule is stated statically (not "the current mode is
        # X") so this block stays byte-stable across mode switches and the
        # system prompt keeps its cache hits.
        return (
            "Sub-agents you can delegate to with the spawn_agent tool (each "
            "runs in isolation and reports back; spawn several in one turn to "
            "fan out independent work):\n\n" + text + "\n\n"
            "Sub-agent reach follows the session mode: outside auto mode, "
            "workspace-mutating tools (write_file, edit_file, bash) are "
            "stripped from every spawn — even from types described as full-"
            "toolset — so sub-agents run read-only there. Don't delegate "
            "edits to a sub-agent unless the session is in auto mode."
        )

    @agent.instructions
    def _mcp_index(ctx: RunContext[Deps]) -> str:
        return mcp_manager.mcp_index_text()

    @agent.instructions
    async def _tool_catalog(ctx: RunContext[Deps]) -> str:
        ws = ctx.deps.workspace
        return await tool_catalog_text(
            mcp_manager, ws.tool_search, ws.tool_search_threshold
        )

    @agent.instructions
    def _memory_policy(ctx: RunContext[Deps]) -> str:
        if proactive_memory:
            return _PROACTIVE_MEMORY_POLICY
        return _ON_REQUEST_MEMORY_POLICY
