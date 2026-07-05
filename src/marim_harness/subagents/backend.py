"""The spawn-backend seam: the parts of a sub-agent run that vary by *backend*
(native in-process Pydantic AI vs the ``claude-cli`` subprocess), factored out of
``SubagentRunner`` so the runner can own ONE foreground/background lifecycle
instead of duplicating it per backend.

``SpawnRun`` is the unified result both backends produce; the runner's shared
finalize tail (stop hook, transcript persist, usage fold, output cap, worktree
close) consumes it without caring which backend ran.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class SpawnRun:
    """The outcome of running one spawn, normalized across backends so the
    runner's finalize tail is backend-agnostic.

    - ``output`` — the sub-agent's final report (before the output cap).
    - ``transcript`` — the full message list to persist to the spawn's sidecar
      (native: the run's ``all_messages()``; CLI: the pre-interrupt prefix +
      the process's emitted transcript).
    - ``usage`` — spend to fold into the session (a ``RunUsage`` for native, a
      ``CliResult.usage`` for CLI; both support ``session.usage += x``).
    - ``final_meta`` — the terminal sidecar meta to stamp (``None`` when the
      spawn had no stream id, i.e. nothing was persisted).
    - ``child_transcripts`` — CLI-only: the demuxed Claude-side Agent/Task
      sub-agents, each keyed by the stream id its live card streamed under, so
      the sub-agents screen can replay them. Empty for native spawns.
    """

    output: str
    transcript: list[Any]
    usage: Any
    final_meta: dict | None = None
    child_transcripts: dict[str, list[Any]] = field(default_factory=dict)
