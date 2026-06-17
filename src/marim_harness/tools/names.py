"""The names of tools a sub-agent may be granted — pure data with no imports.

Kept in a leaf module (rather than in ``provider``) so ``workspace.agents`` can
read the sets without importing ``provider``, which itself imports ``workspace``.
That would otherwise form an import cycle whenever ``provider`` is imported
first."""

# READ_TOOLS are always safe; GATED_TOOLS mutate the workspace and are only
# handed to a sub-agent in auto mode (where they run un-prompted). Memory, skill,
# task, and spawn tools are main-agent only — a sub-agent's job is its task, not
# the session's bookkeeping.
READ_TOOLS = frozenset({"read_file", "glob", "tree", "grep"})
GATED_TOOLS = frozenset({"write_file", "edit_file", "bash"})
SUBAGENT_TOOLS = READ_TOOLS | GATED_TOOLS
