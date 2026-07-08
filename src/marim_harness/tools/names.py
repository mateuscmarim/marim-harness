"""The names of tools a sub-agent may be granted — pure data with no imports.

Kept in a leaf module (rather than in ``provider``) so ``workspace.agents`` can
read the sets without importing ``provider``, which itself imports ``workspace``.
That would otherwise form an import cycle whenever ``provider`` is imported
first."""

# Hard ceiling for nested sub-agent spawning. Main agent is depth 0,
# sub-agents depth 1, grandchildren depth 2. Spawning at depth 2 is
# refused (would produce depth 3).
SUBAGENT_MAX_DEPTH = 3

# Tool reach for sub-agents, split by trust boundary:
#   READ_TOOLS  — local, side-effect-free reads of the workspace; always safe.
#   NET_TOOLS   — outbound network egress (search/fetch). Not workspace-mutating,
#                 but a distinct boundary: an exfiltration/prompt-injection path,
#                 so it's granted deliberately per role, never bundled into READ.
#   GATED_TOOLS — mutate the workspace; only handed to a sub-agent in auto mode
#                 (where they run un-prompted).
# Memory, skill, task, and spawn tools are main-agent only — a sub-agent's job is
# its task, not the session's bookkeeping.
LSP_TOOLS = frozenset({
    "goto_definition", "find_references", "hover",
    "document_symbols", "workspace_symbols", "diagnostics",
})
READ_TOOLS = frozenset({"read_file", "glob", "tree", "grep"}) | LSP_TOOLS
NET_TOOLS = frozenset({"web_search", "fetch_url"})
GATED_TOOLS = frozenset({"write_file", "edit_file", "bash"})
SUBAGENT_TOOLS = READ_TOOLS | NET_TOOLS | GATED_TOOLS

# The five forge (Gitea/GitHub) tool names built by
# tools.forge_tools.build_forge_toolset. Forge tools are main-agent only (not
# in SUBAGENT_TOOLS) and are attached as a separate pydantic-ai toolset rather
# than through BuiltinToolProvider, so they don't live in TOOL_GROUPS above —
# but HarnessBuilder.build() still needs this set to catch a custom-tool name
# collision with a forge name at build() time (see builder.py's collision
# check) instead of failing silently mid-run when with_forge() attaches its
# toolset.
FORGE_TOOLS = frozenset({"list_prs", "view_pr", "ci_status", "create_pr", "checkout_pr"})

# Composition groups for the embeddable builder (see runtime/builder.py). Keys
# MUST mirror provider.ToolGroups' field names — test_provider asserts this.
# "jobs" lists both the four split tools and the combined "job" variant; the
# provider registers one shape or the other, but both belong to the group.
TOOL_GROUPS: dict[str, frozenset[str]] = {
    "files_read": frozenset({"read_file", "glob", "tree", "grep"}),
    "files_write": frozenset({"write_file", "edit_file"}),
    "bash": frozenset({"bash"}),
    "net": NET_TOOLS,
    "memory": frozenset({"remember", "recall"}),
    "skills": frozenset({"activate_skill", "read_skill_file"}),
    "tasks": frozenset({"update_tasks", "ask_user", "present_plan"}),
    "jobs": frozenset({"jobs", "job_output", "wait_for_job", "cancel_job", "job"}),
    "spawn": frozenset({"spawn_agent"}),
}
