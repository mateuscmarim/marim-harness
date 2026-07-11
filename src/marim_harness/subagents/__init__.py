"""Sub-agent execution: the in-process runner and the Claude Code CLI backend.

``runner`` owns spawning isolated sub-agents on behalf of the harness, with the
model-loop retry/overflow/contention recovery factored out into ``run_driver``
and the ``claude -p`` execute/resume path factored out into
``cli_spawn``/``cli_backend``. The package re-exports :class:`SubagentRunner` so
callers keep importing it from ``marim_harness.subagents`` exactly as before
the module split.
"""

from .policies import MaskingPolicy, RetryPolicy
from .runner import SubagentRunner

__all__ = ["MaskingPolicy", "RetryPolicy", "SubagentRunner"]
