"""The names of tools a sub-agent may be granted — pure data with no imports.

Kept in a leaf module (rather than in ``provider``) so ``workspace.agents`` can
read the sets without importing ``provider``, which itself imports ``workspace``.
That would otherwise form an import cycle whenever ``provider`` is imported
first."""

# Tool reach for sub-agents, split by trust boundary:
#   READ_TOOLS  — local, side-effect-free reads of the workspace; always safe.
#   NET_TOOLS   — outbound network egress (search/fetch). Not workspace-mutating,
#                 but a distinct boundary: an exfiltration/prompt-injection path,
#                 so it's granted deliberately per role, never bundled into READ.
#   GATED_TOOLS — mutate the workspace; only handed to a sub-agent in auto mode
#                 (where they run un-prompted).
# Memory, skill, task, and spawn tools are main-agent only — a sub-agent's job is
# its task, not the session's bookkeeping.
READ_TOOLS = frozenset({"read_file", "glob", "tree", "grep"})
NET_TOOLS = frozenset({"web_search", "fetch_url"})
GATED_TOOLS = frozenset({"write_file", "edit_file", "bash"})
SUBAGENT_TOOLS = READ_TOOLS | NET_TOOLS | GATED_TOOLS
