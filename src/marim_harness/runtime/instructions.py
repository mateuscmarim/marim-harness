from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai import RunContext

if TYPE_CHECKING:
    from ..mcp import McpManager
    from ..tools.provider import ToolGroups

from ..advisor import ADVISOR_GUIDANCE
from ..config import config_dir
from ..mcp.catalog import tool_catalog_text
from ..plugins import plugin_instruction_texts
from ..tools.memory_tools import resolve_scope
from ..workspace import (
    agents_index_text,
    discover_agents,
    discover_skills,
    load_index,
    skills_index_text,
)
from .deps import Deps, HarnessAgent

# The stock system prompt for a built harness. Lives here (not bootstrap) so the
# builder and the CLI share one source.
DEFAULT_INSTRUCTIONS = (
    "You are a coding agent operating inside a workspace directory. "
    "Use the provided tools to read, search, and edit files and run commands. "
    "Always read a file before editing it. Keep changes minimal and focused."
)

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
# Keyed by (resolved global scope root str, resolved project scope root str);
# see _memory_index_block.
_MEMORY_INDEX_CACHE: dict = {}

_PROACTIVE_MEMORY_POLICY = (
    "Proactive memory is ON — save durable user preferences, feedback, and "
    "project conventions with remember. Skip recoverable info, one-off details, "
    "secrets, and anything the repo already records (git history, AGENTS.md, "
    "code structure). Update existing entries over adding duplicates; forget "
    "entries that turn out to be wrong."
)

_ON_REQUEST_MEMORY_POLICY = (
    "Save to memory only when the user explicitly asks (e.g. 'remember that…' "
    "or /remember). Do not save proactively. Even then, skip anything the repo "
    "already records (git history, AGENTS.md, code structure)."
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


def _memory_index_block(ctx: RunContext[Deps]) -> str:
    """The injected memory-index block (global then project), or "" if neither
    has a ``MEMORY.md``.

    Scopes are resolved through :func:`resolve_scope` — the same helper
    ``remember``/``recall`` use — so an explicit ``workspace.memory_root``
    (embedders, via HarnessBuilder.with_memory) is honored here too; otherwise
    this is byte-identical to the historical ``global_scope()``/
    ``project_scope(root)`` mapping. Memoized under a stat fingerprint of the
    two ``MEMORY.md`` files so the per-request ``_memory_indexes`` closure
    re-reads them only when one changes. load_index still performs the actual
    read on a miss; we stat the files here purely to key the cache."""
    global_scope_ = resolve_scope(ctx, "global")
    project_scope_ = resolve_scope(ctx, "project")
    paths = [
        global_scope_.root / _MEMORY_INDEX_FILE,
        project_scope_.root / _MEMORY_INDEX_FILE,
    ]
    # Keyed on the resolved scope roots (not just workspace_root) so a
    # memory_root change invalidates the cache too.
    key = (str(global_scope_.root.resolve()), str(project_scope_.root.resolve()))
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


def _scratchpad_block(ctx: RunContext[Deps]) -> str:
    """The scratchpad prompt section, or "" when no scratchpad is available
    (disabled, no session, or the dir can't be provided safely — the getter
    already folded all of those to None). Module-level rather than only a
    closure so it is directly unit-testable. The path is stable within a
    session, so the block doesn't churn the prompt cache turn-to-turn."""
    getter = ctx.deps.services.get_scratchpad
    if getter is None:
        return ""
    path = getter()
    if path is None:
        return ""
    return (
        "Scratchpad directory for this session (outside the workspace):\n"
        f"{path}\n\n"
        "Use it, by absolute path, for temporary and intermediate files — "
        "working scripts, staged outputs, analysis artifacts — instead of "
        "writing them into the workspace. write_file/edit_file writes there "
        "do not need approval. It is removed when the session is deleted and "
        "the OS clears it on reboot, so anything worth keeping belongs in "
        "the workspace."
    )


# --- module-level instruction closures --------------------------------------
#
# These take only ``ctx`` — no free variables from register_instructions — so
# they live at module scope rather than nested inside it. That isn't just
# style: ruff's mccabe C901 check folds a nested function's own complexity
# into its enclosing function's count (verified against the installed ruff:
# a trivial single-branch closure nested inside a function adds to that
# function's reported complexity even when reached through a uniform loop
# rather than an if-chain). Keeping these seven as top-level defs is what
# lets register_instructions' table-driven loop actually collapse its count
# instead of just moving the same branches one level deeper. Only the three
# closures below that must capture ``mcp_manager``/``proactive_memory``
# (``_mcp_index``, ``_tool_catalog``, ``_memory_policy``) stay nested — their
# bodies are branch-light enough that the cost is small.


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


def _project_instructions(ctx: RunContext[Deps]) -> str:
    text = load_project_instructions(ctx.deps.workspace.root)
    if not text:
        return ""
    return f"Project-specific instructions:\n\n{text}"


def _scratchpad(ctx: RunContext[Deps]) -> str:
    return _scratchpad_block(ctx)


def _plugin_instructions(ctx: RunContext[Deps]) -> str:
    # No trust flag is in reach here, so plugin_instruction_texts falls
    # back to MARIM_TRUST_PROJECT_HOOKS itself (same convention as the
    # discover_skills/discover_agents closures below): a cloned repo's
    # committed project-scope plugin can't inject its AGENTS.md until
    # the project is trusted.
    texts = plugin_instruction_texts(ctx.deps.workspace.root)
    if not texts:
        return ""
    blocks = [f"## From plugin '{name}'\n\n{text}" for name, text in texts]
    return (
        "Instructions contributed by installed plugins (treat like "
        "project instructions):\n\n" + "\n\n".join(blocks)
    )


def _memory_indexes(ctx: RunContext[Deps]) -> str:
    return _memory_index_block(ctx)


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


def _advisor_guidance(ctx: RunContext[Deps]) -> str:
    # Gated on the SAME seam that gates the tool (the prepare hook in
    # tools/advisor_tools.py), so the prompt can never advertise a tool that
    # isn't in the schema, or vice versa. Toggling the advisor mid-session
    # therefore changes both together — one prompt-cache break per toggle,
    # inherent to a client-side advisor and accepted (see the design spec).
    if ctx.deps.services.advise is None:
        return ""
    return ADVISOR_GUIDANCE


def register_instructions(
    agent: HarnessAgent, mcp_manager: McpManager, proactive_memory: bool,
    *, global_instructions: bool = True, groups: ToolGroups | None = None,
) -> None:
    """Register all dynamic instruction closures on ``agent``.

    Every closure that advertises or reaches for a tool group is gated on
    that group actually being loaded — a closure describing a tool the model
    can't call is worse than no closure, since the model will try the tool
    anyway and get a hard failure. ``groups`` (the same ``ToolGroups`` the
    provider registers tools from) supplies that gate:

    - ``_agent_index`` (advertises ``spawn_agent`` and the sub-agent roster)
      is gated on ``groups.spawn``.
    - ``_skill_index`` (advertises ``activate_skill``) is gated on
      ``groups.skills``.
    - ``_memory_indexes`` (advertises ``recall``, and reads MEMORY.md to do
      so) is gated on ``groups.memory``.
    - ``_scratchpad`` (advertises that ``write_file``/``edit_file`` writes to
      the scratchpad bypass approval) is gated on ``groups.files_write``.

    ``groups=None`` means "all groups on" — the CLI/bootstrap default, and
    also what a bare ``HarnessConfig()`` gets when constructed directly
    (matching ``BuiltinToolProvider``'s own None-means-all convention, so the
    two never drift independently).

    ``global_instructions`` gates the user-level closure *and*
    ``_plugin_instructions``: both reach into the embedding user's
    ``~/.config/marim`` directory (plugin instructions also read
    project-local ``.marim/plugins``, but plugins themselves are only
    discoverable there once installed through the CLI's global state) — CLI
    keeps this on; HarnessBuilder-embedded harnesses turn it off so a bare
    ``.build()`` never reaches outside the workspace for either. Every other
    closure (project instructions, MCP index, tool catalog, memory policy)
    registers unconditionally — none of them advertise a gateable tool group
    or read outside the workspace/what the caller explicitly opted into.
    """
    spawn_on = groups is None or groups.spawn
    skills_on = groups is None or groups.skills
    memory_on = groups is None or groups.memory
    files_write_on = groups is None or groups.files_write

    def _mcp_index(ctx: RunContext[Deps]) -> str:
        return mcp_manager.mcp_index_text()

    async def _tool_catalog(ctx: RunContext[Deps]) -> str:
        ws = ctx.deps.workspace
        return await tool_catalog_text(
            mcp_manager, ws.tool_search, ws.tool_search_threshold
        )

    def _memory_policy(ctx: RunContext[Deps]) -> str:
        if proactive_memory:
            return _PROACTIVE_MEMORY_POLICY
        return _ON_REQUEST_MEMORY_POLICY

    gated: list[tuple[bool, Callable[[RunContext[Deps]], Any]]] = [
        (global_instructions, _global_instructions),
        (True, _project_instructions),
        (files_write_on, _scratchpad),
        (global_instructions, _plugin_instructions),
        (memory_on, _memory_indexes),
        (skills_on, _skill_index),
        (spawn_on, _agent_index),
        (True, _advisor_guidance),
        (True, _mcp_index),
        (True, _tool_catalog),
        (True, _memory_policy),
    ]
    for enabled, fn in gated:
        if enabled:
            agent.instructions(fn)
