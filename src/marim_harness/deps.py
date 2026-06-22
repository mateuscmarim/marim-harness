from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Awaitable, Callable, Optional

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.tools import DeferredToolApprovalResult

from .command_policy import CommandPolicy

if TYPE_CHECKING:
    from .hooks.dispatch import TurnHooks
    from .hooks.runner import HookRunner
    from .lsp.manager import LspManager
    from .notifications import Notifier

from .ask_user import Question
from .jobs import JobRegistry
from .permissions import Mode
from .tasks import TaskList

ApprovalFn = Callable[[object], Awaitable[DeferredToolApprovalResult | bool]]
# (type, task, stream_id, mcp_names, max_output_chars, model) -> the sub-agent's
# final report. Wired by the Harness.
SubAgentRunner = Callable[
    [str, str, str, Optional[list[str]], Optional[int], Optional[str]],
    Awaitable[str],
]
# (stream_id, event, usage) -> None. Forwards a sub-agent's run events to the UI
# so it can stream them nested under the spawn, tagged with the run's live usage
# (a RunUsage, or None) so the UI can show the token total, cache split, and cost.
SubAgentEventCb = Callable[[str, object, object], Awaitable[None]]
# (type, task, mcp_names, max_output_chars, model) -> the sub-agent's final
# report. Like SubAgentRunner but with no streaming — used to run a sub-agent as
# a detached background job.
BackgroundAgentRunner = Callable[
    [str, str, Optional[list[str]], Optional[int], Optional[str]],
    Awaitable[str],
]

# (questions) -> {header: answer}, where answer is a str (single-select) or a
# list[str] (multi-select); None when the user cancelled. Wired by the TUI; None
# when there's no interactive UI (headless), so the tool degrades gracefully.
AskUserFn = Callable[[list[Question]], Awaitable[Optional[dict]]]


@dataclass
class Deps:
    workspace_root: Path
    mode: Mode = Mode.ask
    request_approval: Optional[ApprovalFn] = None
    # Lets the ask_user tool put a structured question to the user mid-turn. None
    # when headless (the tool then returns a graceful note).
    ask_user: Optional[AskUserFn] = None
    # The session's live checklist. Tools mutate it via ctx.deps; the Harness
    # persists it and the TUI renders it. Every Deps gets its own.
    tasks: TaskList = field(default_factory=TaskList)
    # The session's live background jobs. Tools launch/inspect via ctx.deps; the
    # TUI renders a live panel. Not persisted — process-scoped.
    jobs: JobRegistry = field(default_factory=JobRegistry)
    # Allow/deny policy for shell commands, enforced inside the bash tool in
    # every mode. The default (empty) policy permits everything.
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)
    # Optional Claude-Code-compatible hook engine. None when no hooks.json is
    # configured (every fire-point becomes a cheap ``is None`` no-op).
    hooks: Optional["HookRunner"] = None
    # Optional session-scoped LSP server pool. None when no LSP is wired (every
    # LSP tool becomes a cheap ``is None`` guard returning an unavailable note).
    lsp: Optional["LspManager"] = None
    # Lets the spawn_agent tool launch a sub-agent and stream its events.
    run_subagent: Optional[SubAgentRunner] = None
    on_subagent_event: Optional[SubAgentEventCb] = None
    # Lets spawn_agent(background=True) run a sub-agent as a detached job.
    run_background_agent: Optional[BackgroundAgentRunner] = None
    # Optional desktop notifier. None when notifications are disabled; the TUI
    # and headless runner fire it at key event points (turn complete, error,
    # approval needed, ask user, background job finished).
    notifier: "Optional[Notifier]" = None
    # The session-bound hook dispatcher, set by the Harness so tools (ask_user,
    # update_tasks) can fire lifecycle hooks with a full payload. None until the
    # Harness wires it, or when no hooks are configured.
    turn_hooks: "Optional[TurnHooks]" = None


# The main agent's concrete generic type: deps are ``Deps`` and a turn yields
# either final text or a batch of deferred (approval-gated) tool requests. Shared
# so tool/instruction registration helpers carry the deps type through and a
# ``RunContext[Deps]`` tool checks cleanly. Sub-agents have no approval round, so
# they produce plain ``str``.
HarnessAgent = Agent[Deps, str | DeferredToolRequests]
SubAgent = Agent[Deps, str]
