"""Hook event names (Claude Code's exact strings) and the set of events whose
stdout is read for injected context. Kept dependency-free (a leaf module) so it
can be imported anywhere without risking an import cycle."""

SESSION_START = "SessionStart"
USER_PROMPT_SUBMIT = "UserPromptSubmit"
PRE_TOOL_USE = "PreToolUse"
POST_TOOL_USE = "PostToolUse"
PRE_COMPACT = "PreCompact"
SUBAGENT_START = "SubagentStart"
SUBAGENT_STOP = "SubagentStop"
STOP = "Stop"
SESSION_END = "SessionEnd"

# Only these two events may inject context back into the turn (additionalContext).
INJECTING_EVENTS = frozenset({SESSION_START, USER_PROMPT_SUBMIT})
