from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.settings import ModelSettings

if TYPE_CHECKING:
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.models import Model

    from ..config.model import ModelSource, MultiModelSource

from ..compaction import (
    Summarizer,
    Titler,
    make_summarizer,  # noqa: F401 — re-exported for tests
    make_titler,  # noqa: F401 — re-exported for tests
)
from ..hooks.dispatch import TurnHooks
from ..lsp.manager import LspManager
from ..mcp import McpManager
from ..notifications import NotificationConfig
from ..session import SessionController, SessionManager, SessionStore
from ..session.checkpoints import CheckpointManager
from ..subagents import SubagentRunner
from ..tools.names import SUBAGENT_MAX_DEPTH
from ..tools.provider import ToolProvider
from ..tools.suggest import suggest_unknown_tool_retry
from ..workspace.snapshot import GitSnapshotter
from .context import (
    actionable_error_note as _actionable_error_note,  # noqa: F401 — re-exported for tests
)
from .context import (
    strip_turn_context,  # noqa: F401  — re-exported for session_view + tests
    wrap_turn_context,  # noqa: F401  — re-exported for tests
)
from .controller import (  # noqa: F401 — _has_unanswered_tool_calls/_repair_unanswered_tool_calls re-exported for tests; _drop_nameless_tool_calls used locally by build_collaborators
    TurnController,
    _drop_nameless_tool_calls,
    _has_unanswered_tool_calls,
    _repair_unanswered_tool_calls,
)
from .deps import (
    ApprovalFn,
    AskUserFn,
    Deps,
    HarnessAgent,
    HarnessServices,
    SubAgentEventCb,
    SubAgentModelCb,
    SubAgentNoticeCb,
    SubAgentUsageCb,
)
from .instructions import register_instructions
from .permissions import Mode

logger = logging.getLogger(__name__)

# Force parallel tool calling on for both the main agent and spawned sub-agents.
# It's a base ModelSettings key that each model reads with .get(): providers that
# support it honor it (Anthropic maps it to disable_parallel_tool_use=False;
# OpenAI/Groq/xAI pass it through), and providers that don't simply never read
# the key — so this is "on where available" without breaking anything else.
_DEFAULT_MODEL_SETTINGS = ModelSettings(parallel_tool_calls=True)


@dataclass
class HarnessConfig:
    """Bundles the optional knobs for :class:`Harness`.

    Every field has a sensible default so callers only set what they need.
    ``model_label``, ``model_source``, ``model_id``, ``store``, ``manager``,
    ``max_context_tokens``, ``keep_last_messages``, ``summarizer``, ``titler``,
    ``proactive_memory``, ``mcp_servers``, and ``mcp_disabled`` were formerly
    individual keyword arguments on ``Harness.__init__``.
    """

    model_label: str = "model"
    store: SessionStore | None = None
    manager: SessionManager | None = None
    max_context_tokens: int = 100_000
    keep_last_messages: int = 20
    summarizer: Summarizer | None = None
    titler: Titler | None = None
    model_source: ModelSource | MultiModelSource | None = None
    model_id: str | None = None
    proactive_memory: bool = False
    mcp_servers: list[object] = field(default_factory=list)
    mcp_disabled: set | None = None
    # LSP master switch. False ⇒ no LspManager is built (deps.services.lsp stays None), so
    # diagnostics-on-edit no-ops. Navigation-tool registration is gated separately
    # on the provider (see build_harness), keyed on lsp_enabled and lsp_tools_enabled.
    lsp_enabled: bool = True
    # Autonomous wake-on-completion knobs, surfaced to the TUI app. Defaults
    # match ModelConfig: wake on, cap 8.
    autonomous_wake: bool = True
    wake_depth_cap: int = 8
    # Backstop on a single sub-agent run: the most model requests it may make
    # before pydantic-ai aborts it. A runaway sub-agent (stuck calling tools and
    # never concluding) is bounded rather than blocking the spawning turn forever.
    subagent_request_limit: int = 50
    # How many times a sub-agent run is retried after a transient model error
    # (gateway/server hiccup, request timeout, rate limit) before the failure
    # surfaces. Permanent errors (malformed request, auth) are never retried.
    subagent_retry_attempts: int = 2
    # Cap on how many spawns run their model loop concurrently. A wide fan-out
    # otherwise fires every request at once, tripping a shared route's upstream
    # rate limit; the cap queues the excess. None ⇒ unbounded (historical default).
    subagent_concurrency: int | None = None
    # Maximum number of tokens' worth of messages kept when a sub-agent
    # transcript is written to its sidecar. Older messages are dropped first.
    subagent_transcript_cap: int = 2000
    # Desktop-notification config. Disabled by default; the TUI and headless
    # runner build a Notifier from this and fire at key event points.
    notifications: NotificationConfig = field(default_factory=NotificationConfig.disabled)


def build_services(
    deps: Deps,
    *,
    lsp: LspManager | None,
    turn_hooks: TurnHooks,
    subagents: SubagentRunner,
) -> HarnessServices:
    """Assemble the Harness-wired collaborator container and install it on
    ``deps``. Centralises the one late binding the deps<->services cycle
    requires (see HarnessServices)."""
    services = HarnessServices(
        lsp=lsp,
        turn_hooks=turn_hooks,
        run_subagent=subagents.run,
        run_background_agent=subagents.run_background,
    )
    deps.services = services
    return services


@dataclass(frozen=True)
class Collaborators:
    """The wired collaborator graph for one Harness. Built by
    ``build_collaborators`` so the construction order and the deps<->services
    cycle live in one named, testable place rather than inline in
    ``Harness.__init__``."""

    agent: HarnessAgent
    mcp: McpManager
    lsp: LspManager | None
    session: SessionController
    checkpoints: CheckpointManager
    hooks: TurnHooks
    subagents: SubagentRunner


def build_collaborators(
    model: Model,
    provider: ToolProvider,
    deps: Deps,
    instructions: str,
    cfg: HarnessConfig,
    *,
    get_model: Callable[[], Model],
) -> Collaborators:
    """Build and wire the full collaborator graph for a Harness, in dependency
    order, and install the deps<->services binding via ``build_services``.

    ``get_model`` is supplied by the caller (closing over the live
    ``Harness.current_model``) so a runtime ``/model`` switch is tracked
    without rewiring the sub-agent runner.
    """
    agent = Agent(
        model,
        deps_type=Deps,
        instructions=instructions,
        output_type=[str, DeferredToolRequests],
        # One extra retry past pydantic-ai's default of 1: weaker models
        # often need a second attempt to correct a malformed tool argument
        # before the turn fails with UnexpectedModelBehavior.
        retries=2,
        model_settings=_DEFAULT_MODEL_SETTINGS,
        # History processors run before EVERY model request (including mid-turn
        # tool-loop continuations and retries), so they catch malformations the
        # turn-start sanitizer in run_turn can't see:
        #  - _drop_nameless_tool_calls strips a ToolCallPart whose function name
        #    never streamed (a flaky model/provider emits these live mid-turn);
        #    left in, every provider rejects the next request with "tool_calls[i]
        #    is missing a function name", failing the turn.
        #  - suggest_unknown_tool_retry enriches an unknown-tool rejection with the
        #    nearest registered name (e.g. agents_memory_smart_search for
        #    agentmemory_memory_smart_search) so the retry has a concrete target.
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
        ],
    )
    provider.register(agent)
    mcp = McpManager(cfg.mcp_servers or [], set(cfg.mcp_disabled or []))
    register_instructions(agent, mcp, cfg.proactive_memory)
    # Session-scoped LSP server pool, reachable by the navigation/diagnostics
    # tools through deps. Subagents share this deps object, so they get LSP too.
    lsp = LspManager(deps.workspace.root) if cfg.lsp_enabled else None
    session = SessionController(
        cfg.store, cfg.manager, deps,
        cfg.max_context_tokens, cfg.keep_last_messages,
        cfg.summarizer, cfg.titler,
    )
    # Per-session checkpoints. Wire the real GitSnapshotter so rewind
    # restores working-tree files end-to-end.
    checkpoints = CheckpointManager(session, GitSnapshotter(deps.workspace.root))
    hooks = TurnHooks(deps, session)
    # The spawn_agent tool reaches the runner through Deps, the same way
    # other tools reach shared state. The runner reads the current model via
    # the closure, so a runtime /model switch is tracked without rewiring.
    subagents = SubagentRunner(
        provider, mcp, deps, hooks, session,
        get_model=get_model,
        model_settings=_DEFAULT_MODEL_SETTINGS,
        request_limit=cfg.subagent_request_limit,
        retry_attempts=cfg.subagent_retry_attempts,
        concurrency=cfg.subagent_concurrency,
        transcript_cap=cfg.subagent_transcript_cap,
        max_depth=SUBAGENT_MAX_DEPTH,
        build_model=(
            # Bind the narrowed (non-None) source as a default so the
            # deferred closure keeps it typed; ``cfg.model_source`` alone
            # wouldn't narrow inside a lambda called later.
            (lambda mid, _src=cfg.model_source: _src.build(mid))
            if cfg.model_source is not None else None
        ),
    )
    # One cohesive late binding for the collaborator cycle: TurnHooks and the
    # sub-agent runners hold this deps object, and tools reach them back
    # through ctx.deps.services.
    build_services(deps, lsp=lsp, turn_hooks=hooks, subagents=subagents)
    return Collaborators(
        agent=agent, mcp=mcp, lsp=lsp, session=session,
        checkpoints=checkpoints, hooks=hooks, subagents=subagents,
    )


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model: Model, provider: ToolProvider, deps: Deps, instructions: str,
                 *, config: HarnessConfig | None = None, **kwargs):
        """Create a Harness.

        ``config`` bundles the optional knobs (session store, model identity,
        MCP servers, etc.). For backward compatibility, individual keyword
        arguments (``model_label``, ``store``, ``model_id``, …) are still
        accepted via ``**kwargs`` as a shorthand for building that config.

        Pass *either* ``config=`` *or* legacy kwargs, not both: there is no
        merge (the kwargs would be silently dropped), so mixing them raises
        ``TypeError`` rather than quietly ignoring half the call.
        """
        if config is not None and kwargs:
            raise TypeError(
                "Harness() takes either config= or legacy keyword arguments, "
                f"not both (got config= plus {sorted(kwargs)})"
            )
        cfg = config or HarnessConfig(**kwargs)
        self.deps = deps
        self.provider = provider
        self.model_label = cfg.model_label
        # The model object used for each turn (swappable at runtime), the source
        # that builds new ones, and the id of the active model.
        self.current_model = model
        self.model_source = cfg.model_source
        self.model_id = cfg.model_id
        # Surfaced for the TUI wake scheduler (interactive only).
        self.autonomous_wake = cfg.autonomous_wake
        self.wake_depth_cap = cfg.wake_depth_cap
        # Build the collaborator graph in one named, testable place. get_model
        # closes over self so a runtime /model switch (set_model) is tracked.
        collab = build_collaborators(
            model, provider, deps, instructions, cfg,
            get_model=lambda: self.current_model,
        )
        self.agent = collab.agent
        self.mcp = collab.mcp
        self.lsp = collab.lsp
        self.session = collab.session
        self.checkpoints = collab.checkpoints
        self.hooks = collab.hooks
        self.subagents = collab.subagents
        # The turn-lifecycle orchestrator. Owns all mutable turn-state
        # (pending notes, steer buffer, active RunContext) and drives the
        # run_turn → approval loop → persist pipeline.
        self.turn_controller = TurnController(
            agent=self.agent,
            session=self.session,
            checkpoints=self.checkpoints,
            hooks=self.hooks,
            mcp=self.mcp,
            deps=self.deps,
            get_model=lambda: self.current_model,
        )

    def bind_ui(
        self,
        *,
        request_approval: ApprovalFn | None = None,
        ask_user: AskUserFn | None = None,
        on_subagent_event: SubAgentEventCb | None = None,
        on_subagent_notice: SubAgentNoticeCb | None = None,
        on_subagent_model: SubAgentModelCb | None = None,
        on_subagent_usage: SubAgentUsageCb | None = None,
        on_mode_change: "Callable[[], None] | None" = None,
        on_tasks_changed: Callable[[], None] | None = None,
        on_jobs_changed: Callable[[], None] | None = None,
        on_compact: Callable[[int, int], None] | None = None,
        on_compact_start: Callable[[], None] | None = None,
        on_rename: Callable[[str, str], None] | None = None,
    ) -> None:
        """Wire the interactive UI's callbacks into the harness in one place.

        The TUI app calls this once at construction so the *callback* wiring
        (approval, ask_user, on_change/on_compact/on_rename hooks) lives here
        rather than being assigned field-by-field across the interface layer.
        It does NOT forbid the interface from *reading* harness state: the TUI
        still reads e.g. ``deps.tasks.items`` and ``deps.jobs.list()`` directly
        when rendering. Headless never calls this: the callbacks stay ``None``
        and every reader guards with an ``is None`` check.
        """
        # A UI is attached → this session has a wake loop, so detached fan-out is
        # safe to activate (headless never calls bind_ui and stays inline).
        self.deps.ui.interactive = True
        self.deps.ui.request_approval = request_approval
        self.deps.ui.ask_user = ask_user
        self.deps.ui.on_subagent_event = on_subagent_event
        self.deps.ui.on_subagent_notice = on_subagent_notice
        self.deps.ui.on_subagent_model = on_subagent_model
        self.deps.ui.on_subagent_usage = on_subagent_usage
        self.deps.ui.on_mode_change = on_mode_change
        self.deps.tasks.on_change = on_tasks_changed
        self.deps.jobs.on_change = on_jobs_changed
        self.session.on_compact = on_compact
        self.session.on_compact_start = on_compact_start
        self.session.on_rename = on_rename

    # --- session lifecycle (operations carrying harness-level logic; plain
    # state and persistence live on ``self.session`` and are reached directly) ---

    def resume(self) -> int:
        count = self.session.resume()
        self.checkpoints.reload()
        self._apply_saved_model()
        return count

    def _clear_job_context(self) -> None:
        """Drop finished-job history and any re-stashed jobs digest when the
        conversation context changes (/clear, /new, /switch): they belong to a
        conversation that's no longer active. Running jobs are process-scoped and
        deliberately kept (see JobRegistry.clear_history)."""
        self.deps.jobs.clear_history()
        self.turn_controller.clear_pending_jobs_digest()

    def reset(self) -> None:
        self.session.reset()
        self.checkpoints.clear()
        self._clear_job_context()

    def new_session(self, name: str | None = None) -> None:
        self.session.new_session(name)
        self.checkpoints.reload()
        self._clear_job_context()
        # Apply the model inherited by SessionManager.create() when it
        # differs from the harness's current model.
        saved = self.session.saved_model_id
        if saved and saved != self.model_id:
            self.set_model(saved, persist=False)

    def switch_session(self, session_id: str) -> int:
        count = self.session.switch_session(session_id)
        self.checkpoints.reload()
        self._apply_saved_model()
        self._clear_job_context()
        return count

    async def rename_session(self, name: str | None = None) -> str | None:
        return await self.session.rename(name)

    def set_model(self, model_id: str, *, persist: bool = True) -> None:
        """Switch the active model at runtime. Rebuilds the per-turn model and
        any configured aux agents (summarizer/titler) on the new model, updates
        the label, and records the choice on the session. No-op without a source.
        """
        if self.model_source is None:
            return
        model = self.model_source.build(model_id)
        self.current_model = model
        self.model_id = model_id
        self.model_label = self.model_source.label(model_id)
        self.session.update_model(model)
        if persist:
            self.session.set_model(model_id)

    @property
    def mode(self) -> Mode:
        """The current approval mode (auto/ask/plan)."""
        return self.deps.workspace.mode

    def set_mode(self, mode: Mode) -> None:
        """Set the approval mode. The single write point for ``deps.mode`` so the
        interface layer doesn't poke ``harness.deps`` field-by-field."""
        self.deps.workspace.mode = mode

    def cycle_mode(self) -> Mode:
        """Advance to the next approval mode and return it."""
        self.deps.workspace.mode = self.deps.workspace.mode.cycle()
        return self.deps.workspace.mode

    def _apply_saved_model(self) -> None:
        """Re-point at a session's saved model after loading it, if one differs
        from what's already active."""
        saved = self.session.saved_model_id
        if saved and self.model_source is not None and saved != self.model_id:
            self.set_model(saved, persist=False)

    # --- MCP lifecycle (connection control; server state and grant resolution
    # live on ``self.mcp`` and are reached directly) ---

    async def connect(self) -> dict:
        return await self.mcp.connect()

    async def aclose(self) -> None:
        await self.mcp.aclose()
        lsp = getattr(self, "lsp", None)
        if lsp is not None:
            await lsp.aclose()

    async def disable_server(self, name: str) -> None:
        self.mcp.disable_server(name, self.deps.workspace.root)

    async def enable_server(self, name: str) -> str | None:
        return await self.mcp.enable_server(name, self.deps.workspace.root)

    # --- hooks (observe-only except session_start, which injects context into
    # the next turn; dispatch + payload assembly live on ``self.hooks``) ---

    async def session_start(self, source: str) -> None:
        """Fire the SessionStart hook (``source`` is ``startup``/``resume``/
        ``clear``) and stash any returned context for the next turn's prompt."""
        ctx = await self.hooks.session_start(source)
        if ctx:
            self.turn_controller.apply_session_start_context(ctx)

    async def session_end(self, reason: str = "exit") -> None:
        """Fire the SessionEnd hook on teardown. Observe-only."""
        await self.hooks.session_end(reason)

    def steer(self, text: str,
              attachments: list[tuple[bytes, str]] | None = None) -> None:
        """Delegate to ``turn_controller.steer``."""
        self.turn_controller.steer(text, attachments)

    def take_buffered_steers(
        self,
    ) -> list[tuple[str, list[tuple[bytes, str]] | None]]:
        """Delegate to ``turn_controller.take_buffered_steers``."""
        return self.turn_controller.take_buffered_steers()

    async def run_turn(
        self, prompt: str,
        event_stream_handler: EventStreamHandler[Deps] | None = None,
        attachments: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        return await self.turn_controller.run_turn(prompt, event_stream_handler, attachments)
