"""Sub-agent execution: the in-process runner and the Claude Code CLI backend.

``runner`` owns spawning and driving isolated sub-agents on behalf of the
harness; ``cli_backend`` is the optional ``claude -p`` backend it delegates to.
The package re-exports :class:`SubagentRunner` so callers keep importing it from
``marim_harness.subagents`` exactly as before the split into two modules.
"""

from .runner import SubagentRunner

__all__ = ["SubagentRunner"]
