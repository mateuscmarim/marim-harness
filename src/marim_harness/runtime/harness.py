from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, cast

from pydantic_ai import Agent, DeferredToolRequests
from pydantic_ai.capabilities import AbstractCapability, ProcessHistory
from pydantic_ai.settings import ModelSettings

from ..advisor import ADVISOR_OFF, make_advisor
from ..mcp.discovered_instructions_capability import DiscoveredInstructionsCapability

if TYPE_CHECKING:
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.models import Model

    from ..config.model import ModelSource, MultiModelSource
    from ..forge.backend import ForgeBackend
    from ..stats.ledger import StatsLedger
    from ..trust_surface import ProjectSurface

from ..compaction import (
    Summarizer,
    Titler,
    make_summarizer,  # noqa: F401 — re-exported for tests
    make_titler,  # noqa: F401 — re-exported for tests
)
from ..config.context_limits import ContextLimits
from ..config.model import DEFAULT_SUBAGENT_CONCURRENCY, SubagentTiers
from ..hooks.dispatch import TurnHooks
from ..lsp.manager import LspManager
from ..lsp.provider import LspRegistry
from ..mcp import McpManager
from ..notifications import NotificationConfig
from ..session import SessionController, SessionManager, SessionStore
from ..session.checkpoints import CheckpointManager
from ..subagents import MaskingPolicy, RetryPolicy, SubagentRunner
from ..tools.forge_tools import build_forge_toolset, forge_toolsets
from ..tools.impl.suggest import suggest_unknown_tool_retry
from ..tools.names import SUBAGENT_MAX_DEPTH
from ..tools.provider import ToolGroups, ToolProvider
from ..workspace.catalog import make_supports_images
from ..workspace.scratchpad import ensure_scratchpad
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
    CliActivityCb,
    Deps,
    HarnessAgent,
    HarnessServices,
    OnPresentPlanFn,
    SubAgentEventCb,
    SubAgentModelCb,
    SubAgentNoticeCb,
    SubAgentThinkingCb,
    SubAgentUsageCb,
    WorkflowRunner,
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
    # The stats ledger (per-turn usage JSONL, dual-written workspace+global).
    # None ⇒ no LedgerStatsRecorder is built and add_usage stays a plain
    # in-memory +=; requires ``store`` too (see build_collaborators — a
    # ledger event needs a session id to attribute to).
    stats_ledger: StatsLedger | None = None
    max_context_tokens: int = 100_000
    keep_last_messages: int = 20
    summarizer: Summarizer | None = None
    titler: Titler | None = None
    # When set, compaction also elides older tool-observation payloads in the
    # retained tail to shed tokens (see compaction.mask_stale_observations). Safe
    # for prompt caching because it runs only when compaction already rewrites the
    # cached tail. User-toggleable via the TUI settings / MARIM_MASK_OBSERVATIONS.
    mask_observations: bool = True
    # Masking thresholds: how many recent tool returns to keep intact, and the
    # minimum rendered length below which a return isn't worth masking.
    mask_keep_recent: int = 4
    mask_min_chars: int = 200
    # The window/budget resolver. None ⇒ build_collaborators constructs a
    # discovery-less one from max_context_tokens, preserving the legacy
    # fixed-budget behavior for embedders that never touch the new knobs.
    context_limits: ContextLimits | None = None
    model_source: ModelSource | MultiModelSource | None = None
    model_id: str | None = None
    proactive_memory: bool = False
    mcp_servers: list[object] = field(default_factory=list)
    mcp_disabled: set | None = None
    # Extra pydantic-ai capabilities (AbstractCapability instances — e.g. from
    # pydantic-ai-harness, or your own) appended to the Agent AFTER marim's
    # built-ins, so the built-in history sanitizers always run first. Typed
    # `object` like forge_backend/mcp_servers to keep this dataclass's imports
    # light; build_collaborators casts at the single use site.
    capabilities: list[object] = field(default_factory=list)
    # The project-trust decision McpManager threads into every disable_server/
    # enable_server persist call (mcp.manager.McpManager.trust_project). It
    # must be the SAME decision ``load_mcp_config`` was built with — mismatched
    # trust would let a toggle write to a file the load path never reads back
    # (or the reverse). The CLI preset (bootstrap.build_harness) wires this
    # from ``cfg.trust_project_hooks``, the very value it already passes to
    # ``load_mcp_config``; an embedder composing HarnessBuilder directly has
    # no project-file loading path at all, so False (untrusted) is the correct
    # default — matching load_mcp_config's own default.
    mcp_trust_project: bool = False
    # LSP master switch. False ⇒ no LspManager is built (deps.services.lsp stays None), so
    # diagnostics-on-edit no-ops. Navigation-tool registration is gated separately
    # on the provider (see build_harness), keyed on lsp_enabled and lsp_tools_enabled.
    lsp_enabled: bool = True
    # The assembled LSP registry (bundled + plugin providers), threaded from
    # bootstrap.build_lsp_registry (CLI) or HarnessBuilder.build()'s embedding
    # default. None ⇒ no LspManager is built even when lsp_enabled is True —
    # a HarnessConfig built by hand without a registry gets no LSP, matching
    # "opt-in, nothing implicit" for direct HarnessConfig construction.
    lsp_registry: LspRegistry | None = None
    # Forge (Gitea/GitHub) tools master switch. False ⇒ forge_toolsets returns []
    # and no forge tools are attached to the Agent, regardless of backend
    # availability (tea on PATH + a configured login).
    forge_enabled: bool = True
    # Explicit forge backend (HarnessBuilder.with_forge). When set it bypasses
    # select_backend's tea-on-PATH auto-detection; forge_enabled must still be
    # True for it to attach.
    forge_backend: object | None = None
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
    # rate limit and letting a runaway loop (one live workflow queued a spawn per
    # CHARACTER of a mis-stringified args value) balloon unchecked; the cap queues
    # the excess. Defaults to the shared cap; pass None explicitly for unbounded.
    subagent_concurrency: int | None = DEFAULT_SUBAGENT_CONCURRENCY
    # Maximum number of tokens' worth of messages kept when a sub-agent
    # transcript is written to its sidecar. Older messages are dropped first.
    subagent_transcript_cap: int = 2000
    # Desktop-notification config. Disabled by default; the TUI and headless
    # runner build a Notifier from this and fire at key event points.
    notifications: NotificationConfig = field(default_factory=NotificationConfig.disabled)
    # Programmatic sub-agent definitions (HarnessBuilder.with_subagent). Resolved
    # ahead of workspace discovery by SubagentRunner._resolve_agent.
    extra_agents: tuple = ()
    # Register the user-level global-instructions closure. The CLI keeps this
    # on; the builder turns it off so an embedded harness never reads the
    # embedding user's marim config dir.
    global_instructions: bool = True
    # Gates the instruction closures that advertise a tool group (sub-agent
    # roster for spawn_agent, skill index for activate_skill, memory index
    # for recall) so an embedded harness's prompt never mentions a tool it
    # didn't register — see register_instructions. None ⇒ every group is
    # treated as on, matching BuiltinToolProvider's own None-means-all
    # default; HarnessBuilder.build() always passes its composed ToolGroups
    # explicitly (see builder.py), so this default only matters for a
    # HarnessConfig built by hand (existing direct callers/tests keep their
    # historical "everything registers" behavior unchanged).
    groups: ToolGroups | None = None
    # Session scratchpad master switch. False ⇒ services.get_scratchpad stays
    # None, which degrades everything downstream at once: no prompt block, no
    # extra write root in the file tools, no ask-mode approval bypass.
    scratchpad_enabled: bool = True
    # Dynamic workflows: the run_workflow tool's engine. Enabled by default,
    # but the engine only builds when pydantic-monty is importable (the
    # [workflows] extra); otherwise services.run_workflow stays None and the
    # tool answers with an install hint. MARIM_WORKFLOWS=0 turns it off.
    workflows_enabled: bool = True
    # Ceiling on the wall-clock budget any single run_workflow call may request;
    # per-call requests are clamped to it (see workflows/engine.py).
    workflow_timeout_secs: float = 1800.0
    # The user-curated model per sub-agent tier (cheap/med/high), threaded
    # straight into SubagentRunner(tiers=...). None ⇒ SubagentRunner falls
    # back to its own all-empty SubagentTiers() default.
    subagent_tiers: SubagentTiers | None = None
    # Advisor: the DEFAULT advisor model (provider:slug, or any pydantic-ai
    # model string when no model_source is composed; None = no advisor), the
    # output cap per consultation, and the per-turn call cap (None =
    # unlimited). The session store's advisor_model overrides advisor_model
    # at runtime — see Harness._apply_saved_advisor.
    advisor_model: str | None = None
    advisor_max_tokens: int = 2048
    advisor_max_uses: int | None = None
    # Thinking level (reasoning effort) applied to the main model per turn via
    # ModelSettings.thinking. One of thinking.THINKING_LEVELS, or None (unset).
    # The session store's ``thinking`` overrides this — see
    # Harness._apply_saved_thinking. Sub-agents inherit the live level via the
    # runner's thinking_default closure (get_thinking below).
    thinking_level: str | None = None


def _build_workflow_engine(cfg: HarnessConfig, deps: Deps, subagents: SubagentRunner):
    """The workflow engine, or None when disabled or pydantic-monty is not
    installed. The import is guarded HERE (not in the tool) so availability
    is decided once at build time and the tool only checks the seam."""
    if not cfg.workflows_enabled:
        return None
    try:
        from ..workflows.engine import WorkflowEngine
    except ImportError as exc:
        if exc.name == "pydantic_monty":
            logger.info(
                "workflows unavailable: pydantic-monty not installed "
                "(uv add 'marim-harness[workflows]')"
            )
        else:
            logger.info("workflows unavailable: %s", exc)
        return None
    return WorkflowEngine(deps, subagents.run, timeout_secs=cfg.workflow_timeout_secs)


def build_services(
    deps: Deps,
    *,
    lsp: LspManager | None,
    turn_hooks: TurnHooks,
    subagents: SubagentRunner,
    get_session_id: Callable[[], str | None] | None = None,
    get_scratchpad: Callable[[], Path | None] | None = None,
    run_workflow: WorkflowRunner | None = None,
    supports_images: Callable[[str], Awaitable[bool | None]] | None = None,
) -> HarnessServices:
    """Assemble the Harness-wired collaborator container and install it on
    ``deps``. Centralises the one late binding the deps<->services cycle
    requires (see HarnessServices). ``get_session_id`` is a live getter (the
    caller closes over the session controller) so tools can read the active
    session id without holding the controller; None in tests/headless."""
    services = HarnessServices(
        lsp=lsp,
        turn_hooks=turn_hooks,
        run_subagent=subagents.run,
        run_background_agent=subagents.run_background,
        resume_subagent=subagents.resume_spawn,
        get_session_id=get_session_id,
        get_scratchpad=get_scratchpad,
        run_workflow=run_workflow,
        supports_images=supports_images,
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
    get_thinking: Callable[[], str | None] = lambda: None,
) -> Collaborators:
    """Build and wire the full collaborator graph for a Harness, in dependency
    order, and install the deps<->services binding via ``build_services``.

    ``get_model`` is supplied by the caller (closing over the live
    ``Harness.current_model``) so a runtime ``/model`` switch is tracked
    without rewiring the sub-agent runner.

    ``get_thinking`` is the same kind of live getter (closing over the live
    ``Harness.thinking_level_id``) so a spawned sub-agent inherits the session's
    current thinking level when its own override/spec don't set one. It defaults
    to "no inherited level" for the embedding builder path.
    """
    mcp = McpManager(
        cfg.mcp_servers or [], set(cfg.mcp_disabled or []),
        trust_project=cfg.mcp_trust_project,
    )
    # Forge (Gitea/GitHub) tools: an explicit backend (embedders) attaches
    # directly; otherwise attach only when enabled AND a backend is available
    # (tea on PATH + a configured login); forge_toolsets returns [] otherwise,
    # making toolsets=[] a no-op on the Agent below.
    if cfg.forge_backend is not None and cfg.forge_enabled:
        # forge_backend is typed `object` on HarnessConfig (it's a dataclass
        # field, not a Protocol-typed one — see the field's docstring); the
        # cast asserts what forge_backend's caller contract already requires:
        # an object satisfying ForgeBackend's five async methods.
        forge_ts = [build_forge_toolset(cast("ForgeBackend", cfg.forge_backend))]
    else:
        forge_ts = forge_toolsets(cfg.forge_enabled, deps.workspace.root)
    agent = Agent(
        model,
        deps_type=Deps,
        instructions=instructions,
        output_type=[str, DeferredToolRequests],
        # One extra retry past pydantic-ai's default of 1: weaker models
        # often need a second attempt to correct a malformed tool argument
        # before the turn fails with UnexpectedModelBehavior.
        retries=2,
        # Pinned: pydantic-ai 2.x flipped the default to 'graceful' (finish the
        # in-flight tool batch after a final result). 'early' preserves the v1
        # behavior the approval loop was built against — a final result ends
        # the run immediately, so no gated tool executes after the model has
        # already produced its answer.
        end_strategy="early",
        model_settings=_DEFAULT_MODEL_SETTINGS,
        toolsets=forge_ts,
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
        #  - DiscoveredInstructionsCapability injects discovered servers' usage
        #    instructions into cacheable history so they are prefix-cached and not
        #    re-sent as dynamic instructions on every request.
        # Embedder capabilities (cfg.capabilities) come last: the built-in
        # sanitizers above must see the raw history before any third-party
        # capability (e.g. a pydantic-ai-harness module) transforms it.
        capabilities=[
            ProcessHistory(_drop_nameless_tool_calls),
            ProcessHistory(suggest_unknown_tool_retry),
            DiscoveredInstructionsCapability(mcp),
            *cast("list[AbstractCapability]", cfg.capabilities),
        ],
    )
    provider.register(agent)
    register_instructions(
        agent, mcp, cfg.proactive_memory,
        global_instructions=cfg.global_instructions, groups=cfg.groups,
    )
    # Session-scoped LSP server pool, reachable by the navigation/diagnostics
    # tools through deps. Subagents share this deps object, so they get LSP too.
    # cfg.lsp_registry is normally pre-resolved by the caller: bootstrap always
    # assembles bundled+plugin providers (build_lsp_registry), and
    # HarnessBuilder.build() defaults to the bundled-only registry whenever LSP
    # ends up enabled. A HarnessConfig built by hand (bypassing both — direct
    # Harness()/HarnessConfig() construction, as tests and simple embedders do)
    # can still leave lsp_registry unset; falling back to the bundled registry
    # here keeps that path's default consistent with HarnessConfig's own
    # "every field has a sensible default" contract, matching the pre-Task-7
    # behavior where lsp_enabled alone was sufficient to get LSP.
    lsp_registry = cfg.lsp_registry
    if lsp_registry is None and cfg.lsp_enabled:
        from ..lsp.bundled import bundled_lsp_providers

        lsp_registry = LspRegistry(bundled_lsp_providers())
    lsp = (
        LspManager(deps.workspace.root, registry=lsp_registry)
        if cfg.lsp_enabled and lsp_registry is not None
        else None
    )
    limits = cfg.context_limits or ContextLimits(budget=cfg.max_context_tokens or None)
    # The live model id for threshold resolution: reads the current model each
    # call, so a runtime /model switch re-keys thresholds without rewiring —
    # the same closure trick get_model itself uses.
    get_model_id = lambda: getattr(get_model(), "model_name", None)  # noqa: E731
    # Late-binding holder: LedgerStatsRecorder needs a duration getter that
    # reads the SessionController being constructed right below it (a
    # chicken/egg the closure resolves by reading the holder lazily, once
    # the controller has been appended to it).
    session_holder: list[SessionController] = []
    stats_recorder = None
    if cfg.stats_ledger is not None and cfg.store is not None:
        from ..stats.recorder import LedgerStatsRecorder

        stats_recorder = LedgerStatsRecorder(
            cfg.stats_ledger,
            session_id=cfg.store.session_id,
            get_model_id=get_model_id,
            get_duration_seconds=lambda: (
                session_holder[0].duration_snapshot() if session_holder else None
            ),
        )
    session = SessionController(
        cfg.store, cfg.manager, deps,
        cfg.max_context_tokens, cfg.keep_last_messages,
        cfg.summarizer, cfg.titler,
        mask_observations=cfg.mask_observations,
        mask_keep_recent=cfg.mask_keep_recent,
        mask_min_chars=cfg.mask_min_chars,
        limits=limits,
        get_model_id=get_model_id,
        stats_recorder=stats_recorder,
    )
    session_holder.append(session)
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
        retry=RetryPolicy(
            request_limit=cfg.subagent_request_limit,
            attempts=cfg.subagent_retry_attempts,
        ),
        concurrency=cfg.subagent_concurrency,
        transcript_cap=cfg.subagent_transcript_cap,
        max_depth=SUBAGENT_MAX_DEPTH,
        extra_agents=cfg.extra_agents,
        tiers=cfg.subagent_tiers,
        # Inherited thinking level for spawns whose override/spec don't set
        # one — read lazily per spawn so a /think switch reaches later spawns.
        thinking_default=get_thinking,
        # Sub-agents share the session's context-limits RESOLVER and masking
        # knobs: one discovery cache governs both the main history and spawned
        # runs, but each spawn resolves its own threshold through it — a
        # per-spawn model override resolves that model's window/budget.
        masking=MaskingPolicy(
            limits=limits,
            enabled=cfg.mask_observations,
            keep_recent=cfg.mask_keep_recent,
            min_chars=cfg.mask_min_chars,
        ),
        build_model=(
            # Bind the narrowed (non-None) source as a default so the
            # deferred closure keeps it typed; ``cfg.model_source`` alone
            # wouldn't narrow inside a lambda called later.
            (lambda mid, _src=cfg.model_source: _src.build(mid))
            if cfg.model_source is not None else None
        ),
    )
    # Live like get_session_id below: a session switch swaps session.store,
    # and the scratchpad must follow the active session. ensure_scratchpad
    # re-mkdirs on every call, so a /tmp cleaned under a resumed session is
    # transparently recreated.
    get_scratchpad = None
    if cfg.scratchpad_enabled:
        def _get_scratchpad() -> Path | None:
            sid = session.store.session_id if session.store is not None else None
            if sid is None:
                return None
            return ensure_scratchpad(deps.workspace.root, sid)
        get_scratchpad = _get_scratchpad
    # The run_workflow tool's engine. Guarded build: disabled by config, or
    # pydantic-monty simply not installed (the [workflows] extra).
    workflow_engine = _build_workflow_engine(cfg, deps, subagents)
    # Vision gate for read_file image returns: catalog-backed when a model
    # source is composed (CLI path), None for explicit-model embedders
    # (HarnessBuilder) — where unknown capability sends images optimistically.
    supports_images = (
        make_supports_images(cfg.model_source.list_models)
        if cfg.model_source is not None
        else None
    )
    # One cohesive late binding for the collaborator cycle: TurnHooks and the
    # sub-agent runners hold this deps object, and tools reach them back
    # through ctx.deps.services.
    build_services(
        deps,
        lsp=lsp,
        turn_hooks=hooks,
        subagents=subagents,
        # Live getter: closes over the controller so a session switch (which swaps
        # ``session.store``) is reflected without rewiring services.
        get_session_id=lambda: session.store.session_id if session.store is not None else None,
        get_scratchpad=get_scratchpad,
        run_workflow=workflow_engine.run if workflow_engine is not None else None,
        supports_images=supports_images,
    )
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
        # Set by bootstrap: the project's gated surface, and — when no trust
        # decision exists anywhere and the surface is non-empty — the payload the
        # TUI's first-open TrustPanel renders. None for embedders (HarnessBuilder
        # does no workspace scanning) and once a decision exists.
        self.project_surface: ProjectSurface | None = None
        self.trust_prompt: ProjectSurface | None = None
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
        # Live thinking level (reasoning effort). Set before build_collaborators
        # and TurnController so their get_thinking closures capture a live
        # attribute; the real value is resolved by _apply_saved_thinking below.
        self._thinking_env_default = cfg.thinking_level
        self.thinking_level_id: str | None = None
        # Build the collaborator graph in one named, testable place. get_model
        # closes over self so a runtime /model switch (set_model) is tracked.
        collab = build_collaborators(
            model, provider, deps, instructions, cfg,
            get_model=lambda: self.current_model,
            get_thinking=lambda: self.thinking_level_id,
        )
        self.agent = collab.agent
        self.mcp = collab.mcp
        self.lsp = collab.lsp
        self.session = collab.session
        self.checkpoints = collab.checkpoints
        self.hooks = collab.hooks
        self.subagents = collab.subagents
        # The workflow runner as built (None when disabled at launch or
        # pydantic-monty is missing). Kept so set_workflows_enabled can
        # restore the seam after a live disable — services.run_workflow
        # itself is the mutable on/off switch the tool checks per call.
        self._workflow_runner = deps.services.run_workflow if deps.services else None
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
            lsp_toolset=self.provider.lsp_toolset(),
            get_model=lambda: self.current_model,
            get_thinking=lambda: self.thinking_level_id,
        )
        # Advisor: build ONE advise callable for the harness lifetime; which
        # model it consults is re-resolved PER CALL through the closure over
        # advisor_model_id, so /advisor switches apply to the next
        # consultation with no rebuild. services.advise is the live on/off
        # seam (the run_workflow pattern): the tool's prepare hook and the
        # steering-instructions closure both read it per request.
        self._advisor_env_default = cfg.advisor_model
        self.advisor_model_id: str | None = None
        self.deps.advisor_max_uses = cfg.advisor_max_uses
        self._advise_fn = make_advisor(
            self._build_advisor_model,
            lambda: self.advisor_model_id,
            cwd=str(deps.workspace.root),
            max_tokens=cfg.advisor_max_tokens,
        )
        self._apply_saved_advisor()
        self._apply_saved_thinking()

    def bind_ui(
        self,
        *,
        request_approval: ApprovalFn | None = None,
        ask_user: AskUserFn | None = None,
        on_subagent_event: SubAgentEventCb | None = None,
        on_subagent_notice: SubAgentNoticeCb | None = None,
        on_subagent_model: SubAgentModelCb | None = None,
        on_subagent_thinking: SubAgentThinkingCb | None = None,
        on_subagent_usage: SubAgentUsageCb | None = None,
        on_cli_activity: CliActivityCb | None = None,
        on_ttft: Callable[[float], None] | None = None,
        on_mode_change: Callable[[], None] | None = None,
        on_present_plan: OnPresentPlanFn | None = None,
        on_workflow_spawn: Callable[[str, str, str, str], Awaitable[None]] | None = None,
        on_workflow_log: Callable[[str, str], None] | None = None,
        on_workflow_spawn_done: Callable[[str, str], None] | None = None,
        on_workflow_start: Callable[[str, str], None] | None = None,
        on_workflow_done: Callable[[str, str, bool], None] | None = None,
        on_tasks_changed: Callable[[], None] | None = None,
        on_jobs_changed: Callable[[], None] | None = None,
        on_compact: Callable[[int, int], None] | None = None,
        on_compact_start: Callable[[], None] | None = None,
        on_notice: Callable[[str], None] | None = None,
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
        self.deps.ui.on_subagent_thinking = on_subagent_thinking
        self.deps.ui.on_subagent_usage = on_subagent_usage
        self.deps.ui.on_cli_activity = on_cli_activity
        self.deps.ui.on_ttft = on_ttft
        self.wire_cli_model(self.current_model)
        self.deps.ui.on_mode_change = on_mode_change
        self.deps.ui.on_present_plan = on_present_plan
        self.deps.ui.on_workflow_spawn = on_workflow_spawn
        self.deps.ui.on_workflow_log = on_workflow_log
        self.deps.ui.on_workflow_spawn_done = on_workflow_spawn_done
        self.deps.ui.on_workflow_start = on_workflow_start
        self.deps.ui.on_workflow_done = on_workflow_done
        self.deps.tasks.on_change = on_tasks_changed
        self.deps.jobs.on_change = on_jobs_changed
        self.session.on_compact = on_compact
        self.session.on_compact_start = on_compact_start
        self.session.on_notice = on_notice
        self.session.on_rename = on_rename

    # --- session lifecycle (operations carrying harness-level logic; plain
    # state and persistence live on ``self.session`` and are reached directly) ---

    def resume(self) -> int:
        count = self.session.resume()
        self.checkpoints.reload()
        self._apply_saved_model()
        self._apply_saved_advisor()
        self._apply_saved_thinking()
        return count

    def _clear_job_context(self) -> None:
        """Drop finished-job history, any re-stashed jobs digest, queued `!`
        passthrough results, and the prior turn's one-shot error note / unconsumed
        hook context when the conversation context changes (/clear, /new,
        /switch): they belong to a conversation that's no longer active. Running
        jobs are process-scoped and deliberately kept (see
        JobRegistry.clear_history). The error-note / hook-context clear closes a
        leak where a failed turn's note (or a departing session's SessionStart
        context) would prepend itself onto the first prompt of a different
        conversation."""
        self.deps.jobs.clear_history()
        self.turn_controller.clear_pending_jobs_digest()
        self.turn_controller.clear_pending_shell_results()
        self.turn_controller.clear_pending_context()

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
        self._apply_saved_advisor()
        self._apply_saved_thinking()

    def switch_session(self, session_id: str) -> int:
        # Clear the OUTGOING session's job context BEFORE loading the incoming one.
        # The clear belongs to the session we're leaving: _clear_job_context wipes
        # jobs.history (plus the digest / `!` passthrough buffers), which are the
        # departing conversation's state. self.session.switch_session then imports
        # the INCOMING session's persisted jobs history (via _load_active_store →
        # jobs.import_history) — and that import must survive. Clearing AFTER the
        # switch (the old order) wiped the freshly imported history, leaving
        # finish_replayed_cards' settled join empty and — worse — making the next
        # persist write jobs=[] back over the file, erasing it for good.
        #
        # But switch_session can RAISE (SessionLoadError: a corrupt/missing store):
        # the controller then stays on the OUTGOING session, yet we've already
        # wiped its job history. The next persist would write jobs=[] over the
        # still-active session's file — the very permanent loss the ordering above
        # guards against, reintroduced through the error path. Snapshot the
        # outgoing history first and restore it if the load fails, so a failed
        # switch is a true no-op for the session we never actually left.
        saved_jobs = self.deps.jobs.export_settled()
        self._clear_job_context()
        try:
            count = self.session.switch_session(session_id)
        except Exception:  # noqa: BLE001,F841
            # Load failed — we're still on the outgoing session. Put its job
            # history back (import_history reloads it as read-only history, the
            # same shape a persist reads) so the next persist doesn't erase the
            # file. The digest / `!` / error-note buffers stay cleared: they are
            # transient per-turn state, not persisted conversation history, so a
            # failed switch dropping them is harmless.
            self.deps.jobs.import_history(saved_jobs)
            raise
        self.checkpoints.reload()
        self._apply_saved_model()
        self._apply_saved_advisor()
        self._apply_saved_thinking()
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
        # A model switch invalidates discovered windows: the switch may land
        # on ANOTHER provider entirely (qualified `local:...` ids) whose
        # catalog must be consulted, and on the local provider the new model
        # JIT-loads, possibly at a different context size than anything probed
        # before. Re-discovery re-probes every active provider's source lazily
        # at the next async site (maybe_compact / spawn prep) — set_model
        # stays sync.
        if self.session.limits is not None:
            self.session.limits.invalidate()
        self.model_id = model_id
        self.model_label = self.model_source.label(model_id)
        self.session.update_model(model)
        if persist:
            self.session.set_model(model_id)
        # Re-wire the late-bound hooks if the new model is a ClaudeCliModel, so
        # switching TO this provider at runtime honors live /mode, the workspace
        # cwd, and the TUI tool-card side-channel.
        self.wire_cli_model(model)

    def wire_cli_model(self, model: Model) -> None:
        """Bind the late-bound hooks a ``ClaudeCliModel`` needs — live approval
        mode, the real workspace (or worktree) cwd, the TUI tool-card side-channel,
        and the sub-agents-screen side-channels for Claude's own Agent/Task spawns.
        A no-op for every other provider's model. Public because ``bootstrap``
        (the CLI preset) binds it once after build, before any UI attaches — the
        internal set_model/bind_ui callers use it too."""
        from ..config.claude_cli_model import ClaudeCliModel

        if isinstance(model, ClaudeCliModel):
            model.mode_getter = lambda: self.mode.value
            model.cwd = str(self.deps.workspace.root)
            model.on_activity = self.deps.ui.on_cli_activity
            model.on_subagent = self.deps.ui.on_subagent_event
            model.on_subagent_model = self.deps.ui.on_subagent_model

    def _build_advisor_model(self, model_id: str) -> Model:
        """Build the advisor's model: through the active model source when one
        exists (cross-provider qualified slugs, the same routing /model uses),
        else pydantic-ai's stock ``infer_model`` — so an embedded harness
        without a source can still pass standard model strings to
        ``with_advisor``. Errors propagate to make_advisor, which folds them
        into the advice-unavailable string."""
        if self.model_source is not None:
            return self.model_source.build(model_id)
        from pydantic_ai.models import infer_model

        return infer_model(model_id)

    def _resolve_advisor_id(self) -> str | None:
        """Session override → env/config default → None. The "off" sentinel is
        itself an override: it beats a configured default, so an explicit
        disable survives restarts distinguishably from "unset"."""
        saved = self.session.saved_advisor_id
        if saved == ADVISOR_OFF:
            return None
        return saved or self._advisor_env_default

    def _apply_saved_advisor(self) -> None:
        """Point the advisor seam at the active session's choice. Called at
        build and after every session change (resume/new/switch), mirroring
        ``_apply_saved_model``."""
        self.advisor_model_id = self._resolve_advisor_id()
        if self.deps.services is not None:
            self.deps.services.advise = (
                self._advise_fn if self.advisor_model_id is not None else None
            )

    def set_advisor_model(self, model_id: str | None, *, persist: bool = True) -> None:
        """Switch the advisor at runtime (None = disable). Unlike set_model
        this is safe mid-turn: resolution is per-consultation, so a switch
        simply applies to the next advisor call; the prepare hook and the
        steering block follow ``services.advise`` on the next model request
        (breaking the prompt cache once — inherent to a client-side advisor)."""
        self.advisor_model_id = model_id
        if self.deps.services is not None:
            self.deps.services.advise = (
                self._advise_fn if model_id is not None else None
            )
        if persist:
            self.session.set_advisor(model_id if model_id is not None else ADVISOR_OFF)

    def get_thinking(self) -> str | None:
        """The live thinking level (session override → env/config default →
        None). Read by the controller per round and the sub-agent runner per
        spawn, so a switch applies without a rebuild."""
        return self.thinking_level_id

    def _resolve_thinking_id(self) -> str | None:
        """Session override → env/config default → None. A persisted level
        (including "off") beats the env default; None means "unset — inherit
        the env default"."""
        saved = self.session.saved_thinking_id
        return saved if saved is not None else self._thinking_env_default

    def _apply_saved_thinking(self) -> None:
        """Point the live thinking level at the active session's choice. Called
        at build and after every session change (resume/new/switch), mirroring
        ``_apply_saved_model``. No seam to flip: the controller and runner read
        thinking_level_id lazily per round/spawn."""
        self.thinking_level_id = self._resolve_thinking_id()

    def set_thinking_level(self, level: str, *, persist: bool = True) -> None:
        """Switch the thinking level at runtime (a member of
        thinking.THINKING_LEVELS, "off" to disable). Safe mid-turn: the level is
        read per round, so a switch simply applies to the next turn/spawn."""
        self.thinking_level_id = level
        if persist:
            self.session.set_thinking(level)

    @property
    def mode(self) -> Mode:
        """The current approval mode (auto/ask/plan)."""
        return self.deps.workspace.mode

    def set_mode(self, mode: Mode, *, persist: bool = False) -> None:
        """Set the approval mode. The single write point for ``deps.mode`` so
        the interface layer doesn't poke ``harness.deps`` field-by-field.
        persist defaults to False: the TUI's mode toggle/cycle is a live,
        per-launch setting (see session/store.py's SessionStore.mode
        docstring) and must keep behaving that way; only the server's
        live-switch path opts in."""
        self.deps.workspace.mode = mode
        if persist:
            self.session.set_mode(mode.value)

    def cycle_mode(self, *, persist: bool = False) -> Mode:
        """Advance to the next approval mode and return it."""
        self.deps.workspace.mode = self.deps.workspace.mode.cycle()
        if persist:
            self.session.set_mode(self.deps.workspace.mode.value)
        return self.deps.workspace.mode

    def set_workflows_enabled(self, enabled: bool) -> bool:
        """Turn dynamic workflows on/off for this session by flipping the
        ``services.run_workflow`` seam the tool checks per call — no rebuild,
        the tool stays registered and degrades to its unavailable hint.
        Returns whether the seam now matches the request: enabling when no
        engine was built at launch (workflows off, or pydantic-monty missing)
        has nothing to restore, so it reports False and the caller can say
        "applies next launch" instead of pretending."""
        if self.deps.services is not None:
            self.deps.services.run_workflow = self._workflow_runner if enabled else None
        return (self._workflow_runner is not None) or not enabled

    def set_subagent_tiering_enabled(self, enabled: bool) -> None:
        """Turn sub-agent model tiering on/off for this session by flipping the
        runner's live tier set. Off ⇒ new spawns inherit the main model; the
        curated per-tier slugs are preserved, so re-enabling restores routing
        without re-entry. In-flight sub-agents keep the model they were built
        with — only spawns started after the flip see the change (the runner
        reads ``self._tiers`` per spawn)."""
        self.subagents.set_tiering_enabled(enabled)

    async def apply_project_trust(self) -> None:
        """Hot-apply a project-trust grant: flip the live TrustState, then
        eagerly reload what loads at startup (hooks config, project MCP
        servers, LSP registry). Lazy readers (skills/agents discovery,
        instructions) pick the flip up on their next read. Idempotent.
        Persistence is the CALLER's job (record_decision) — this seam is
        pure runtime state, so tests and embedders can drive it without
        touching the operator's store."""
        # Idempotence check, not a lock: two overlapping awaits could both get
        # past this guard. Every caller today sits on a single-threaded command
        # path (TUI panel//trust, serve's per-workspace loop), and a double
        # apply is merely wasteful, not corrupting — add a latch only if a
        # genuinely concurrent caller ever appears.
        if self.deps.trust.project:
            return
        from ..hooks import HookRunner, load_hooks_config
        from ..mcp import build_mcp_servers, load_mcp_config
        from .bootstrap import build_lsp_registry  # lazy: bootstrap imports this module

        ws = self.deps.workspace.root
        self.deps.trust.project = True
        self.deps.trust.source = "store"
        self.trust_prompt = None
        hooks_cfg = load_hooks_config(ws, trust_project=True)
        self.deps.hooks = HookRunner(hooks_cfg) if hooks_cfg else None
        if self.mcp is not None:
            specs = load_mcp_config(ws, trust_project=True)
            servers, warnings = build_mcp_servers(specs)
            for warning in warnings:
                logger.warning("MCP config: %s", warning)
            self.mcp.trust_project = True
            await self.mcp.add_servers(servers)
        if self.lsp is not None:
            self.lsp.set_registry(build_lsp_registry(ws, trust_project=True))

    def revoke_project_trust(self) -> None:
        """Flip the live TrustState off and drop project hooks. Already-running
        MCP servers / LSP providers keep running until restart — the caller
        owns telling the user that caveat (and persisting the decision)."""
        self.deps.trust.project = False
        self.deps.trust.source = "store"
        self.trust_prompt = None
        from ..hooks import HookRunner, load_hooks_config

        hooks_cfg = load_hooks_config(self.deps.workspace.root, trust_project=False)
        self.deps.hooks = HookRunner(hooks_cfg) if hooks_cfg else None
        if self.mcp is not None:
            # McpManager.persist_server_enabled() uses trust_project to decide
            # write-trust; the load-trust used here must match, or toggling
            # a server later would persist into the untrusted project's .marim/mcp.json.
            self.mcp.trust_project = False

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

    def add_shell_result(self, command: str, output: str) -> None:
        """Queue a user-run `!` passthrough result for the next turn's context.
        Delegates to the turn controller's pending queue."""
        self.turn_controller.add_shell_result(command, output)

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

    async def manual_compact(self, instructions: str | None = None) -> bool:
        """Manual /compact entry point. Delegates to the turn controller so the
        checkpoint-invalidation wrapper stays the single place every compaction
        is funneled through — a bare ``session.maybe_compact`` here would skip it
        and leave stale checkpoints that a later /rewind would slice mid-pair."""
        return await self.turn_controller.manual_compact(instructions=instructions)
