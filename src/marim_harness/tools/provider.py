"""Registration surface for marim's built-in tools.

The tool *implementations* live in cohesive sibling modules by concern
(``fs_tools``, ``net_tools``, ``lsp_tools``, ``memory_tools``, ``skill_tools``,
``planning_tools``, ``edit_tools``, ``spawn_tools``, ``job_tools``). Each is a
module-level function so it can be registered two ways from one source of truth:
onto the main agent (gated tools behind ``requires_approval=True``) and onto
sub-agents (registered *plain* — reach is decided up front by which names are
granted, never by mid-run prompting).

This module keeps only the wiring: the ``ToolProvider`` protocol, the
``BuiltinToolProvider`` that registers the toolsets, and ``_SUBAGENT_FNS`` (the
name→impl map for sub-agent grants). It references the tools *through their
concern modules* (``edit_tools.bash``, …) rather than re-importing them by name,
so it stays a registry — not a facade to import tools through. Import a tool from
the module that owns it (``from ..tools.edit_tools import bash``).
"""

from collections.abc import Iterable
from typing import TYPE_CHECKING, Protocol

from ..runtime.deps import HarnessAgent, SubAgent

if TYPE_CHECKING:
    from pydantic_ai.toolsets import FunctionToolset

    from ..runtime.deps import Deps
from . import (
    edit_tools,
    fs_tools,
    job_tools,
    lsp_tools,
    memory_tools,
    net_tools,
    planning_tools,
    skill_tools,
    spawn_tools,
)

# Re-exported for backward compatibility; defined in the leaf module ``names``
# so importers (e.g. workspace.agents) don't pull in all of ``provider`` and
# form an import cycle.
from .names import (  # noqa: F401
    GATED_TOOLS,
    LSP_TOOLS,
    NET_TOOLS,
    READ_TOOLS,
    SUBAGENT_MAX_DEPTH,
    SUBAGENT_TOOLS,
)

# Name -> implementation for the tools a sub-agent may receive. The Harness
# decides which names to grant; register_subagent registers exactly those.
_SUBAGENT_FNS = {
    "read_file": fs_tools.read_file,
    "glob": fs_tools.glob,
    "tree": fs_tools.tree,
    "grep": fs_tools.grep,
    "goto_definition": lsp_tools.goto_definition,
    "find_references": lsp_tools.find_references,
    "hover": lsp_tools.hover,
    "document_symbols": lsp_tools.document_symbols,
    "workspace_symbols": lsp_tools.workspace_symbols,
    "diagnostics": lsp_tools.diagnostics,
    "web_search": net_tools.web_search,
    "fetch_url": net_tools.fetch_url,
    "write_file": edit_tools.write_file,
    "edit_file": edit_tools.edit_file,
    "bash": edit_tools.bash,
}


class ToolProvider(Protocol):
    """Registers a set of tools onto an Agent. The swap point for future
    pydantic-ai-harness FileSystem/Shell capabilities."""

    def register(self, agent: HarnessAgent) -> None:
        ...

    def register_subagent(self, agent: SubAgent, tool_names: Iterable[str]) -> None:
        ...

    def lsp_toolset(self) -> "FunctionToolset[Deps] | None":
        ...


class BuiltinToolProvider:
    """Hand-written fs + shell tools backed by the pure functions in this package."""

    def __init__(self, *, register_lsp_tools: bool = True,
                 combined_job_tool: bool = False) -> None:
        """``register_lsp_tools`` gates the six LSP navigation tools for both the
        main agent and sub-agents. The harness derives it from the LSP config
        (``lsp_enabled and lsp_tools_enabled``); diagnostics-on-edit is wired
        separately through ``deps.services.lsp`` and is unaffected by this flag.

        ``combined_job_tool`` (prototype) swaps the four job tools
        (jobs/job_output/wait_for_job/cancel_job) for a single ``job(action, …)``
        tool. Job tools are main-agent only, so this affects ``register`` only."""
        self._register_lsp_tools = register_lsp_tools
        self._combined_job_tool = combined_job_tool

    def register(self, agent: HarnessAgent) -> None:
        """Register the full main-agent toolset: read tools, the memory / skill /
        task / spawn tools, and the workspace-mutating tools behind approval."""
        # Registered individually rather than via a loop: each tool has a distinct
        # signature, and a loop variable unions them into a type the .tool()
        # overloads can't resolve.
        agent.tool(fs_tools.read_file)
        agent.tool(fs_tools.glob)
        agent.tool(fs_tools.tree)
        agent.tool(fs_tools.grep)
        # Outbound network tools are gated (like write/edit/bash), not ungated
        # like the local reads above: they are an exfiltration boundary (see
        # names.NET_TOOLS). Gating routes them through resolve_approvals, so auto
        # mode still runs them un-prompted (frictionless), ask mode prompts per
        # call, and — the point — plan mode denies them instead of silently
        # allowing an un-approved fetch that could carry a secret off the host.
        agent.tool(requires_approval=True)(net_tools.web_search)
        agent.tool(requires_approval=True)(net_tools.fetch_url)
        agent.tool(memory_tools.remember)
        agent.tool(memory_tools.recall)
        agent.tool(skill_tools.activate_skill)
        agent.tool(skill_tools.read_skill_file)
        agent.tool(planning_tools.update_tasks)
        agent.tool(planning_tools.ask_user)
        agent.tool(planning_tools.present_plan)
        # The nesting ceiling isn't bound here: it rides on Deps
        # (subagent_max_depth), where the model can't touch it.
        agent.tool(spawn_tools.spawn_agent)
        if self._combined_job_tool:
            agent.tool(job_tools.job)
        else:
            agent.tool(job_tools.jobs)
            agent.tool(job_tools.job_output)
            agent.tool(job_tools.wait_for_job)
            agent.tool(job_tools.cancel_job)
        agent.tool(requires_approval=True)(edit_tools.write_file)
        agent.tool(requires_approval=True)(edit_tools.edit_file)
        agent.tool(requires_approval=True)(edit_tools.bash)

    def lsp_toolset(self) -> "FunctionToolset[Deps] | None":
        """The LSP navigation tools as a deferrable toolset for the *main* agent,
        or None when LSP tools are disabled. Built from the same
        ``_register_lsp_tools`` flag that used to gate their static registration,
        so the two never drift. The Harness injects the result into TurnController,
        which routes it through ``compose_turn_toolsets`` per turn. Sub-agents are
        unaffected — ``register_subagent`` still name-registers LSP."""
        return lsp_tools.build_lsp_toolset() if self._register_lsp_tools else None

    def register_subagent(self, agent: SubAgent, tool_names: Iterable[str]) -> None:
        """Register exactly ``tool_names`` onto a sub-agent. Gated tools are
        registered *plain* (no approval round) — reach is decided up front by
        which names the Harness grants, not by prompting mid-run. spawn_agent is
        never among them — nested spawning is granted separately by
        SubagentRunner.build, and only above the leaf depth."""
        for name in sorted(set(tool_names)):
            if not self._register_lsp_tools and name in LSP_TOOLS:
                continue
            fn = _SUBAGENT_FNS.get(name)
            if fn is None:
                continue
            agent.tool(fn)
