"""The spawn-backend seam: the parts of a sub-agent run that vary by *backend*
(native in-process Pydantic AI vs the ``claude-cli`` subprocess), factored out of
``SubagentRunner`` so the runner can own ONE foreground/background lifecycle
instead of duplicating it per backend.

``SpawnRun`` is the unified result both backends produce; the runner's shared
finalize tail (stop hook, transcript persist, usage fold, output cap, worktree
close) consumes it without caring which backend ran.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any, Protocol

from pydantic_ai.usage import RunUsage

if TYPE_CHECKING:
    from .isolation import SpawnWorktree

# The resume prompt every interrupted spawn continues from — shared by the
# native resume path (runner.resume_spawn) and the CLI resume path
# (cli_spawn.CliSpawnOrchestrator.resume); it lives on this leaf module so
# neither importer needs the other.
CONTINUATION_PROMPT = (
    "You were interrupted before finishing. The conversation above is your "
    "own earlier progress on this task — continue from where it leaves off "
    "and finish the task, then report as usual."
)


@dataclass
class SpawnRun:
    """The outcome of running one spawn, normalized across backends so the
    runner's finalize tail is backend-agnostic.

    - ``output`` — the sub-agent's final report (before the output cap).
    - ``transcript`` — the full message list to persist to the spawn's sidecar
      (native: the run's ``all_messages()``; CLI: the pre-interrupt prefix +
      the process's emitted transcript).
    - ``usage`` — spend to fold into the session: a ``RunUsage`` either way,
      since ``cli_backend.synth_usage`` builds one from the CLI process's
      reported usage block too, so ``session.add_usage(x)`` always applies.
    - ``final_meta`` — the terminal sidecar meta to stamp (``None`` when the
      spawn had no stream id, i.e. nothing was persisted).
    - ``child_transcripts`` — CLI-only: the demuxed Claude-side Agent/Task
      sub-agents, each keyed by the stream id its live card streamed under, so
      the sub-agents screen can replay them. Empty for native spawns.
    """

    output: str
    transcript: list[Any]
    usage: RunUsage
    final_meta: dict | None = None
    child_transcripts: dict[str, list[Any]] = field(default_factory=dict)


class SpawnLifecycle(Protocol):
    """The exact call shape of ``SubagentRunner._run_spawn_lifecycle``, bound.
    A Protocol (rather than a bare ``Callable[...]``) so the runner<->cli_spawn
    seam keeps arity/kwarg checking that ``Callable[...]`` would erase."""

    def __call__(
        self, run_fn: Callable[[], Awaitable[SpawnRun]], *, iso: SpawnWorktree | None,
        resumed: bool, background: bool, name: str, stop_task: str, note: str,
        max_output_chars: int | None, stream_id: str,
        timing: tuple[float, float, list[float]] | None = None,
    ) -> Awaitable[str]: ...
