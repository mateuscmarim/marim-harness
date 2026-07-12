from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from dataclasses import replace as dataclass_replace
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.tools import DeferredToolApprovalResult

from ..command_policy import CommandPolicy

if TYPE_CHECKING:
    from ..hooks.dispatch import TurnHooks
    from ..hooks.runner import HookRunner
    from ..lsp.manager import LspManager
    from ..notifications import Notifier

from ..ask_user import Choice, Question
from ..jobs import JobRegistry
from ..tasks import TaskList
from ..tools.names import SUBAGENT_MAX_DEPTH
from ..workspace.fs import ReadLedger
from .permissions import Mode

ApprovalFn = Callable[[object], Awaitable[DeferredToolApprovalResult | bool]]
# (type, task, stream_id, mcp_names, max_output_chars, model, isolation,
#  caller_depth) -> the sub-agent's final report. Wired by the Harness.
# ``caller_depth`` is the depth of the agent *calling* spawn_agent (0 for the
# main agent, 1 for a depth-1 sub-agent, …); the spawned child runs at
# caller_depth + 1. It must come from the caller's deps, not the runner's own.
SubAgentRunner = Callable[
    [str, str, str, list[str] | None, int | None, str | None,
     str | None, int],
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
# (type, task, mcp_names, max_output_chars, model, isolation, stream_id,
#  caller_depth) -> the sub-agent's final report. Like SubAgentRunner; when
# stream_id is set (the spawn's tool_call_id) the detached run also streams its
# events to the UI (Phase 2). ``caller_depth`` propagates the caller's depth the
# same way the foreground runner does, so a background spawn from a sub-agent
# lands at the right depth and can't bypass the nesting ceiling.
BackgroundAgentRunner = Callable[
    [str, str, list[str] | None, int | None, str | None, str | None, str, int],
    Awaitable[str],
]
# (stream_id) -> (job_id, message). Lets the sub-agents screen resume an
# interrupted spawn from its persisted transcript as a background job: a
# non-None job_id on success (message is a user-renderable confirmation), or
# None with a user-renderable refusal reason otherwise.
ResumeSubagent = Callable[[str], Awaitable[tuple[str | None, str]]]
# (script, args, tool_call_id) -> the workflow's final result, shaped for the
# model. None when workflows are disabled (MARIM_WORKFLOWS=0) or
# pydantic-monty is not installed — the run_workflow tool returns an install
# hint in that case. Wired by the Harness (see _build_workflow_engine).
WorkflowRunner = Callable[[str, object, str], Awaitable[str]]

# (questions) -> {header: answer}, where answer is a str (single-select) or a
# list[str] (multi-select); None when the user cancelled. Wired by the TUI; None
# when there's no interactive UI (headless), so the tool degrades gracefully.
AskUserFn = Callable[[list[Question]], Awaitable[dict | None]]

# (summary, steps, choices) -> the user's decision (chosen label + optional
# revise-feedback). Wired by the TUI (mounts a PlanCard inline panel); None when
# headless, where present_plan falls back to ask_user then to "save and stay in
# plan mode". The choices are passed through so the card never hardcodes the
# plan-execution labels (their single source of truth is
# tools/planning_tools._PLAN_CHOICES).
OnPresentPlanFn = Callable[[str, list[str], list[Choice]], Awaitable["PlanDecision"]]

# (events) -> None. Renders a claude-cli model's own tool_use/tool_result as
# display-only native tool cards in the MAIN transcript. The claude-cli provider
# delegates a turn to `claude -p` and returns text only (Claude runs its own
# tools), so this side-channel is how that activity reaches the UI WITHOUT passing
# through pydantic_ai's agent graph. None when headless (activity folds to ▸ text).
CliActivityCb = Callable[[list], Awaitable[None]]


@dataclass
class HarnessServices:
    """Collaborator handles wired by the Harness after construction.

    This container forms a reference cycle with ``Deps``: ``TurnHooks`` and
    the sub-agent runners hold the ``deps`` object, while tools reach them back
    through ``ctx.deps.services``. The cycle makes one late binding
    unavoidable — the Harness builds these, then assigns the populated
    container onto ``deps.services`` in a single step (see ``build_services``
    in ``harness.py``).
    Every field is optional: headless runs and tests leave them ``None`` and
    each tool guards with an ``is None`` check.
    """

    # Session-scoped LSP server pool. None when LSP is disabled.
    lsp: "LspManager | None" = None
    # Session-bound hook dispatcher, so tools (ask_user, update_tasks) can fire
    # lifecycle hooks with a full payload. None when no hooks are configured.
    turn_hooks: "TurnHooks | None" = None
    # Lets the spawn_agent tool launch a sub-agent and stream its events.
    run_subagent: SubAgentRunner | None = None
    # Lets spawn_agent(background=True) run a sub-agent as a detached job.
    run_background_agent: BackgroundAgentRunner | None = None
    # Lets the sub-agents screen resume an interrupted spawn from its persisted
    # transcript as a background job (spec 2026-07-03-subagent-resume, §4).
    resume_subagent: "ResumeSubagent | None" = None
    # Lets the run_workflow tool execute a model-authored orchestration
    # script in the Monty sandbox. None ⇒ workflows unavailable.
    run_workflow: WorkflowRunner | None = None
    # Returns the active session's id live (it changes on session switch), or None
    # when no session is active. Lets a tool stamp session-scoped artifacts (e.g.
    # plan files) without reaching into the session controller. None in headless /
    # tests, where callers fall back to a workspace-derived id.
    get_session_id: Callable[[], str | None] | None = None
    # Returns the active session's scratchpad directory (created on demand),
    # or None when scratchpads are disabled, no session is active, or the dir
    # can't be provided safely (see workspace/scratchpad.py). Live for the
    # same reason as get_session_id: the session id changes on switch. The
    # file tools widen their path guard with it; the approval resolver
    # auto-approves writes into it; an instructions closure advertises it.
    get_scratchpad: Callable[[], Path | None] | None = None


@dataclass
class WorkspaceConfig:
    """Workspace identity plus the session's approval mode. Every field but
    ``mode`` is set once at construction and never mutated; ``mode`` is live —
    Harness.set_mode/cycle_mode rewrite it in place so every reader (tools,
    sub-agent reach, plan gating) sees the switch immediately."""

    root: Path
    mode: Mode = Mode.ask
    command_policy: CommandPolicy = field(default_factory=CommandPolicy)
    tool_search: str = "auto"
    tool_search_threshold: int = 15
    # Embedder overrides (set by HarnessBuilder; None everywhere in the CLI):
    # an explicit memory store root replacing the XDG-global/.marim-project
    # scopes, and explicit skill directories replacing skill discovery.
    memory_root: Path | None = None
    skill_dirs: "tuple[Path, ...] | None" = None


@dataclass(frozen=True)
class PlanDecision:
    """The outcome of a present_plan handoff. ``choice`` is one of the
    _PLAN_CHOICES labels (or the "Keep planning" dismiss label). ``feedback`` is
    the user's revise-notes when they typed feedback instead of picking a choice
    — always paired with the "Keep planning" choice (reject-and-revise)."""

    choice: str
    feedback: str | None = None


@dataclass(frozen=True)
class CurrentPlan:
    """The plan narrative from the most recent present_plan this session: the
    summary paragraph, the ordered steps, and the plan-file path (None if the
    write failed). Step *progress* is NOT here — it lives in ``Deps.tasks``, the
    single source of truth for done/in-progress/pending. This holds only what
    the pinned TaskPanel title and the PlanScreen overlay need to show the
    'why' after the transient PlanCard scrolls away."""

    summary: str
    steps: list[str]
    path: str | None


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
    on_cli_activity: CliActivityCb | None = None
    # Latest streamed request's time-to-first-token, in seconds. Reported by
    # the TtftTrackingModel wrapper the controller adds when this is set.
    on_ttft: "Callable[[float], None] | None" = None
    on_mode_change: "Callable[[], None] | None" = None
    on_present_plan: "OnPresentPlanFn | None" = None
    # (stream_id, type, task, parent_tool_call_id) -> None. Fired by the
    # workflow engine BEFORE each child spawn so the TUI can claim a card for
    # a stream id that has no spawn_agent tool call to intercept (cards are
    # otherwise created only when a literal spawn_agent call renders).
    on_workflow_spawn: "Callable[[str, str, str, str], Awaitable[None]] | None" = None
    # (message) -> None. A workflow script's log() line. None when headless
    # (the engine falls back to DEBUG logging).
    on_workflow_log: "Callable[[str], None] | None" = None
    detach_fanout: bool = False
    interactive: bool = False
    notifier: "Notifier | None" = None


@dataclass
class Deps:
    workspace: WorkspaceConfig
    ui: UIHooks = field(default_factory=UIHooks)
    tasks: TaskList = field(default_factory=TaskList)
    jobs: JobRegistry = field(default_factory=JobRegistry)
    # The most recent plan presented this session (present_plan sets it); read
    # by the TaskPanel title and the PlanScreen overlay. None until a plan is
    # presented. Narrative only — step progress lives in ``tasks``.
    plan: "CurrentPlan | None" = None
    services: HarnessServices = field(default_factory=HarnessServices)
    hooks: "HookRunner | None" = None
    # Per-session read-before-edit ledger: which files have been read (and their
    # fingerprint at read time) so edit_file/write_file can refuse to modify a
    # file the agent hasn't seen, or that changed on disk since it was read.
    # Shared with sub-agents through ``replace(deps, …)``; it's keyed by resolved
    # path, so an isolated worktree spawn still must read its own copies first.
    reads: ReadLedger = field(default_factory=ReadLedger)
    # Zero-indexed nesting depth: 0 for the main agent, 1 for its sub-agents,
    # 2 for grandchildren. Used by spawn_agent to enforce the depth limit.
    subagent_depth: int = 0
    # The nesting ceiling spawn_agent enforces (spawning at depth d is refused
    # when d + 1 >= this). Deliberately NOT a tool parameter: anything in the
    # advertised schema is model-writable, so exposing it would let the model
    # raise its own ceiling. SubagentRunner stamps its configured ceiling here
    # when building a child's deps.
    subagent_max_depth: int = SUBAGENT_MAX_DEPTH

    def replace(self, **kw) -> "Deps":
        """Return a shallow copy with specified fields replaced."""
        return dataclass_replace(self, **kw)


# The main agent's concrete generic type: deps are ``Deps`` and a turn yields
# either final text or a batch of deferred (approval-gated) tool requests. Shared
# so tool/instruction registration helpers carry the deps type through and a
# ``RunContext[Deps]`` tool checks cleanly. Sub-agents have no approval round, so
# they produce plain ``str``.
HarnessAgent = Agent[Deps, str | DeferredToolRequests]
SubAgent = Agent[Deps, str]
