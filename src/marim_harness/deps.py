from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

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
# (type, task, stream_id, mcp_names, max_output_chars, model, isolation) -> the
# sub-agent's final report. Wired by the Harness.
SubAgentRunner = Callable[
    [str, str, str, list[str] | None, int | None, str | None,
     str | None],
    Awaitable[str],
]
# (stream_id, event, usage) -> None. Forwards a sub-agent's run events to the UI
# so it can stream them nested under the spawn, tagged with the run's live usage
# (a RunUsage, or None) so the UI can show the token total, cache split, and cost.
SubAgentEventCb = Callable[[str, object, object], Awaitable[None]]
# (stream_id, message) -> None. A short out-of-band status line for a foreground
# spawn's card (e.g. "transient error — retrying 1/2…"), distinct from the run's
# own streamed events. None when there's no UI listening.
SubAgentNoticeCb = Callable[[str, str], Awaitable[None]]
# (type, task, mcp_names, max_output_chars, model, isolation, stream_id) -> the
# sub-agent's final report. Like SubAgentRunner; when stream_id is set (the spawn's
# tool_call_id) the detached run also streams its events to the UI (Phase 2).
BackgroundAgentRunner = Callable[
    [str, str, list[str] | None, int | None, str | None, str | None, str],
    Awaitable[str],
]

# (questions) -> {header: answer}, where answer is a str (single-select) or a
# list[str] (multi-select); None when the user cancelled. Wired by the TUI; None
# when there's no interactive UI (headless), so the tool degrades gracefully.
AskUserFn = Callable[[list[Question]], Awaitable[dict | None]]


@dataclass
class HarnessServices:
    """Collaborator handles wired by the Harness after construction.

    These four form a reference cycle with ``Deps``: ``TurnHooks`` and the
    sub-agent runners hold the ``deps`` object, while tools reach them back
    through ``ctx.deps.services``. The cycle makes one late binding
    unavoidable — the Harness builds these, then assigns the populated
    container onto ``deps.services`` in a single step (see ``agent.py``).
    Every field is optional: headless runs and tests leave them ``None`` and
    each tool guards with an ``is None`` check.
    """

    # Session-scoped LSP server pool. None when LSP is disabled.
    lsp: Optional["LspManager"] = None
    # Session-bound hook dispatcher, so tools (ask_user, update_tasks) can fire
    # lifecycle hooks with a full payload. None when no hooks are configured.
    turn_hooks: Optional["TurnHooks"] = None
    # Lets the spawn_agent tool launch a sub-agent and stream its events.
    run_subagent: SubAgentRunner | None = None
    # Lets spawn_agent(background=True) run a sub-agent as a detached job.
    run_background_agent: BackgroundAgentRunner | None = None


@dataclass
class Deps:
    workspace_root: Path
    mode: Mode = Mode.ask
    # Detached fan-out routing. detach_fanout is the config knob; interactive is
    # set True only when a UI is attached (bind_ui) — both required before
    # spawn_agent auto-detaches, since headless has no wake loop to synthesize.
    detach_fanout: bool = False
    interactive: bool = False
    request_approval: ApprovalFn | None = None
    # Lets the ask_user tool put a structured question to the user mid-turn. None
    # when headless (the tool then returns a graceful note).
    ask_user: AskUserFn | None = None
    # The session's live checklist. Tools mutate it via ctx.deps; the Harness
    # persists it and the TUI renders it. Every Deps gets its own.
    tasks: TaskList = field(default_factory=TaskList)
    # The session's live background jobs. Tools launch/inspect via ctx.deps; the
    # TUI renders a live panel. Not persisted — process-scoped.
    jobs: JobRegistry = field(default_factory=JobRegistry)
    # Allow/deny policy for shell commands, enforced inside the bash tool in
    # every mode. The default (empty) policy permits everything.
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)
    # Collaborator handles wired by the Harness after construction. Its own
    # container so the late-bound services are separated from caller inputs.
    services: HarnessServices = field(default_factory=HarnessServices)
    # Optional Claude-Code-compatible hook engine. None when no hooks.json is
    # configured (every fire-point becomes a cheap ``is None`` no-op).
    hooks: Optional["HookRunner"] = None
    on_subagent_event: SubAgentEventCb | None = None
    on_subagent_notice: SubAgentNoticeCb | None = None
    # Optional desktop notifier. None when notifications are disabled; the TUI
    # and headless runner fire it at key event points (turn complete, error,
    # approval needed, ask user, background job finished).
    notifier: "Notifier | None" = None


# The main agent's concrete generic type: deps are ``Deps`` and a turn yields
# either final text or a batch of deferred (approval-gated) tool requests. Shared
# so tool/instruction registration helpers carry the deps type through and a
# ``RunContext[Deps]`` tool checks cleanly. Sub-agents have no approval round, so
# they produce plain ``str``.
HarnessAgent = Agent[Deps, str | DeferredToolRequests]
SubAgent = Agent[Deps, str]
