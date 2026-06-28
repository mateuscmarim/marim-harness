from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.tools import DeferredToolApprovalResult

from ..command_policy import CommandPolicy

if TYPE_CHECKING:
    from ..hooks.dispatch import TurnHooks
    from ..hooks.runner import HookRunner
    from ..lsp.manager import LspManager
    from ..notifications import Notifier

from ..ask_user import Question
from ..jobs import JobRegistry
from ..tasks import TaskList
from .permissions import Mode

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
# (stream_id, model) -> None. Surfaces the real model a sub-agent actually ran on
# — e.g. the model a claude-cli spawn reports in its stream — so the spawn card
# shows it instead of falling back to the harness's own model. None when no UI.
SubAgentModelCb = Callable[[str, str], Awaitable[None]]
# (stream_id, usage) -> None. Delivers the final RunUsage for a CLI spawn (which
# can only report usage once, at the end of its run) so the card and pane show
# the token total, cache split, and cost. None when no UI.
SubAgentUsageCb = Callable[[str, object], Awaitable[None]]
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
class WorkspaceConfig:
    """Immutable workspace identity. Set once at construction, never mutated."""

    root: Path
    mode: Mode = Mode.ask
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)


@dataclass
class UIHooks:
    """UI callbacks wired by bind_ui(). All None when headless.

    SubAgentCallbacks fields are absorbed here -- they are UI-layer concerns
    (streaming sub-agent events to the TUI) and don't belong on the core
    runtime object.
    """

    request_approval: ApprovalFn | None = None
    ask_user: AskUserFn | None = None
    on_subagent_event: SubAgentEventCb | None = None
    on_subagent_notice: SubAgentNoticeCb | None = None
    on_subagent_model: SubAgentModelCb | None = None
    on_subagent_usage: SubAgentUsageCb | None = None
    detach_fanout: bool = False
    interactive: bool = False
    notifier: "Notifier | None" = None


@dataclass
class Deps:
    workspace: WorkspaceConfig
    ui: UIHooks = field(default_factory=UIHooks)
    tasks: TaskList = field(default_factory=TaskList)
    jobs: JobRegistry = field(default_factory=JobRegistry)
    services: HarnessServices = field(default_factory=HarnessServices)
    hooks: "HookRunner | None" = None


# The main agent's concrete generic type: deps are ``Deps`` and a turn yields
# either final text or a batch of deferred (approval-gated) tool requests. Shared
# so tool/instruction registration helpers carry the deps type through and a
# ``RunContext[Deps]`` tool checks cleanly. Sub-agents have no approval round, so
# they produce plain ``str``.
HarnessAgent = Agent[Deps, str | DeferredToolRequests]
SubAgent = Agent[Deps, str]
