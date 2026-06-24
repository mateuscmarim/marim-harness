from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import RunContext

if TYPE_CHECKING:
    from .mcp import McpManager

from .config import config_dir
from .deps import Deps, HarnessAgent
from .plugins import plugin_instruction_texts
from .workspace import (
    agents_index_text,
    discover_agents,
    discover_skills,
    global_scope,
    load_index,
    project_scope,
    skills_index_text,
)

_PROJECT_INSTRUCTIONS_FILE = "AGENTS.md"
_PROJECT_FALLBACK_FILES = ("AGENTS.md", "CLAUDE.md")

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
    """
    candidates = [filename] if filename is not None else _PROJECT_FALLBACK_FILES

    for name in candidates:
        path = Path(workspace_root) / name
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
        text = load_project_instructions(ctx.deps.workspace_root)
        if not text:
            return ""
        return f"Project-specific instructions:\n\n{text}"

    @agent.instructions
    def _plugin_instructions(ctx: RunContext[Deps]) -> str:
        texts = plugin_instruction_texts(ctx.deps.workspace_root)
        if not texts:
            return ""
        blocks = [f"## From plugin '{name}'\n\n{text}" for name, text in texts]
        return (
            "Instructions contributed by installed plugins (treat like "
            "project instructions):\n\n" + "\n\n".join(blocks)
        )

    @agent.instructions
    def _memory_indexes(ctx: RunContext[Deps]) -> str:
        parts = []
        g = load_index(global_scope())
        if g:
            parts.append(f"# User memory (global)\n\n{g}")
        p = load_index(project_scope(ctx.deps.workspace_root))
        if p:
            parts.append(f"# Project memory\n\n{p}")
        if not parts:
            return ""
        return (
            "Memory index (use recall for full entries, "
            "remember to save):\n\n" + "\n\n".join(parts)
        )

    @agent.instructions
    def _skill_index(ctx: RunContext[Deps]) -> str:
        text = skills_index_text(discover_skills(ctx.deps.workspace_root))
        if not text:
            return ""
        return (
            "Available skills below — each is a packaged workflow. When a "
            "task matches one's description, load its full instructions with "
            "the activate_skill tool (by name) and follow them.\n\n" + text
        )

    @agent.instructions
    def _agent_index(ctx: RunContext[Deps]) -> str:
        text = agents_index_text(discover_agents(ctx.deps.workspace_root))
        if not text:
            return ""
        return (
            "Sub-agents you can delegate to with the spawn_agent tool (each "
            "runs in isolation and reports back; spawn several in one turn to "
            "fan out independent work):\n\n" + text
        )

    @agent.instructions
    def _mcp_index(ctx: RunContext[Deps]) -> str:
        return mcp_manager.mcp_index_text()

    @agent.instructions
    def _memory_policy(ctx: RunContext[Deps]) -> str:
        if proactive_memory:
            return _PROACTIVE_MEMORY_POLICY
        return _ON_REQUEST_MEMORY_POLICY
