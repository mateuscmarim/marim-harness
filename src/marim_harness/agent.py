from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import Agent, DeferredToolRequests, capture_run_messages
from pydantic_ai.capabilities import ProcessHistory
from pydantic_ai.messages import BinaryContent, ModelMessage
from pydantic_ai.settings import ModelSettings
from pydantic_ai.usage import RunUsage

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.models import Model

    from .config.model import ModelSource

from .compaction import (
    Summarizer,
    Titler,
    make_summarizer,
    make_titler,
)
from .deps import Deps, HarnessAgent, HarnessServices
from .errors import dump_provider_error, is_context_overflow_error
from .hooks.dispatch import TurnHooks
from .instructions import register_instructions
from .lsp.manager import LspManager
from .mcp import McpManager
from .notifications import NotificationConfig
from .permissions import Mode, resolve_approvals
from .session import SessionController, SessionManager, SessionStore
from .session.checkpoints import CheckpointManager
from .subagents import SubagentRunner
from .tools.provider import ToolProvider
from .tools.suggest import suggest_unknown_tool_retry
from .turn_context import (
    actionable_error_note as _actionable_error_note,
)
from .turn_context import (
    render_checklist_block,
    strip_turn_context,  # noqa: F401  — re-exported for session_view + tests
    wrap_turn_context,
)
from .workspace.snapshot import GitSnapshotter

logger = logging.getLogger(__name__)

# Force parallel tool calling on for both the main agent and spawned sub-agents.
# It's a base ModelSettings key that each model reads with .get(): providers that
# support it honor it (Anthropic maps it to disable_parallel_tool_use=False;
# OpenAI/Groq/xAI pass it through), and providers that don't simply never read
# the key — so this is "on where available" without breaking anything else.
_DEFAULT_MODEL_SETTINGS = ModelSettings(parallel_tool_calls=True)


def _has_unanswered_tool_calls(history: list[ModelMessage]) -> bool:
    """True when some ToolCallPart in ``history`` has no matching ToolReturnPart.
    Such a history ends an exchange mid-flight, and every provider rejects an
    unanswered tool_use on the next request — so persisting one makes the
    session unresumable until it's manually cleared."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    calls: set = set()
    returns: set = set()
    for message in history:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart):
                calls.add(part.tool_call_id)
            elif isinstance(part, ToolReturnPart):
                returns.add(part.tool_call_id)
    return bool(calls - returns)


def _drop_nameless_tool_calls(history: list[ModelMessage]) -> list[ModelMessage]:
    """Return a history with every nameless ``ToolCallPart`` (and the returns it
    orphans) removed. A flaky model/provider can stream a partial tool call whose
    function name never arrives, leaving a ``ToolCallPart`` with an empty
    ``tool_name``; persisted, every provider then rejects the next request
    ("tool_calls[i] is missing a function name"), wedging the session just like a
    dangling call does. The unanswered-call repair can't catch it — the part has
    an id, it's just nameless — so it needs its own pass. A ``ToolReturnPart``
    that answered a dropped call is dropped too (it would now reference nothing),
    and a message left with no parts is removed rather than sent empty. Returns
    the input list unchanged when nothing is nameless, so callers can skip a
    redundant persist."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    nameless_ids = {
        part.tool_call_id
        for message in history
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolCallPart) and not part.tool_name
    }
    if not nameless_ids:
        return history
    cleaned: list = []
    for message in history:
        parts = getattr(message, "parts", None)
        if parts is None:
            cleaned.append(message)
            continue
        kept = [
            part
            for part in parts
            if not (
                isinstance(part, (ToolCallPart, ToolReturnPart))
                and part.tool_call_id in nameless_ids
            )
        ]
        if not kept:
            continue  # the malformed call was all this message carried — drop it
        if len(kept) != len(parts):
            message = replace(message, parts=kept)
        cleaned.append(message)
    return cleaned


def _turn_produced_response(history: list[ModelMessage], since: int) -> bool:
    """True if the turn that began at history index ``since`` produced at least one
    model response. A turn that failed before reaching a response leaves only its
    (flushed) bare user prompt after ``since`` — no ``ModelResponse`` — so its
    start-of-turn checkpoint is a dead rewind target and is rolled back."""
    from pydantic_ai.messages import ModelResponse

    return any(isinstance(m, ModelResponse) for m in history[since:])


_INTERRUPTED_TOOL_NOTE = (
    "Tool call was interrupted before completion and did not run (the turn was "
    "aborted). Re-issue it if you still need the result."
)


def _repair_unanswered_tool_calls(history: list[ModelMessage]) -> list[ModelMessage]:
    """Return a history in which every ToolCallPart has a matching ToolReturnPart,
    synthesizing an interrupted-tool return for any that lack one. An aborted
    turn (API failure, usage limit, cancel) can leave a ToolCallPart with no
    return; every provider then rejects the next request, so a session persisted
    in that state is unresumable until repaired. The synthesized return is placed
    in a ModelRequest right after the response that made the call, so it stays
    valid for providers that require results to immediately follow their call.
    Returns the input list unchanged when nothing is dangling, so callers can
    skip a redundant persist."""
    from pydantic_ai.messages import (
        ModelRequest,
        ModelResponse,
        ToolCallPart,
        ToolReturnPart,
    )

    answered = {
        part.tool_call_id
        for message in history
        for part in getattr(message, "parts", [])
        if isinstance(part, ToolReturnPart)
    }
    repaired: list = []
    changed = False
    for message in history:
        repaired.append(message)
        if not isinstance(message, ModelResponse):
            continue
        missing = [
            part
            for part in message.parts
            if isinstance(part, ToolCallPart) and part.tool_call_id not in answered
        ]
        if not missing:
            continue
        repaired.append(
            ModelRequest(
                parts=[
                    ToolReturnPart(
                        tool_name=part.tool_name,
                        content=_INTERRUPTED_TOOL_NOTE,
                        tool_call_id=part.tool_call_id,
                    )
                    for part in missing
                ]
            )
        )
        answered.update(part.tool_call_id for part in missing)
        changed = True
    return repaired if changed else history


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
    model_source: ModelSource | None = None
    model_id: str | None = None
    proactive_memory: bool = False
    mcp_servers: list[object] = field(default_factory=list)
    mcp_disabled: set | None = None
    # LSP master switch. False ⇒ no LspManager is built (deps.services.lsp stays None), so
    # diagnostics-on-edit no-ops. Navigation-tool registration is gated separately
    # on the provider (see build_harness), keyed on lsp_enabled and lsp_tools_enabled.
    lsp_enabled: bool = True
    # Autonomous wake-on-completion knobs, surfaced to the TUI app. Defaults
    # match ModelConfig: wake on, cap 3.
    autonomous_wake: bool = True
    wake_depth_cap: int = 3
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
    get_model: Callable[[], Any],
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
    lsp = LspManager(deps.workspace_root) if cfg.lsp_enabled else None
    session = SessionController(
        cfg.store, cfg.manager, deps,
        cfg.max_context_tokens, cfg.keep_last_messages,
        cfg.summarizer, cfg.titler,
    )
    # Per-session checkpoints. Wire the real GitSnapshotter so rewind
    # restores working-tree files end-to-end.
    checkpoints = CheckpointManager(session, GitSnapshotter(deps.workspace_root))
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

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
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
        # A one-shot note about the last actionable failure, prepended to the
        # next turn's prompt so the model knows it didn't complete (see
        # _actionable_error_note). None when there's nothing to surface.
        self._pending_error_note: str | None = None
        # One-shot context returned by a SessionStart hook, prepended to the next
        # turn's prompt and consumed there (mirrors _pending_error_note).
        self._pending_hook_context: str | None = None
        # A finished-jobs digest re-stashed after a failed turn. The digest is
        # normally drained from deps.jobs at assembly time (clearing that buffer),
        # so a turn that then fails would lose it forever; we capture it for this
        # turn and re-stash it here on failure so the next turn re-emits it. None
        # when there's nothing carried over (the common path drains live).
        self._pending_jobs_digest: str | None = None
        # What this turn's _assemble_prompt consumed (hook context + jobs digest),
        # held only for the duration of the run so the failure path can restore
        # the one-shot consumables. See _assemble_prompt / run_turn.
        self._consumed_this_turn: tuple[str | None, str | None] = (None, None)
        # Live RunContext of the in-flight turn, captured by the event-stream
        # handler wrapper; None between turns. A steer enqueues onto it.
        self._active_run_ctx: RunContext[Deps] | None = None
        # Steers typed when no run is live yet (ask-mode between-round gap):
        # (text, attachments) buffered, flushed when a ctx is next captured.
        self._steer_buffer: list[tuple[str, list[tuple[bytes, str]] | None]] = []
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

    def bind_ui(
        self,
        *,
        request_approval: Callable[..., Any] | None = None,
        ask_user: Callable[..., Any] | None = None,
        on_subagent_event: Callable[..., Any] | None = None,
        on_subagent_notice: Callable[..., Any] | None = None,
        on_subagent_model: Callable[..., Any] | None = None,
        on_tasks_changed: Callable[..., Any] | None = None,
        on_jobs_changed: Callable[..., Any] | None = None,
        on_compact: Callable[..., Any] | None = None,
        on_compact_start: Callable[..., Any] | None = None,
        on_rename: Callable[..., Any] | None = None,
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
        self.deps.interactive = True
        self.deps.request_approval = request_approval
        self.deps.ask_user = ask_user
        self.deps.on_subagent_event = on_subagent_event
        self.deps.on_subagent_notice = on_subagent_notice
        self.deps.on_subagent_model = on_subagent_model
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
        self._pending_jobs_digest = None

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
        if (
            self.session.store is not None
            and self.session.store.model
            and self.session.store.model != self.model_id
        ):
            self.set_model(self.session.store.model, persist=False)

    def switch_session(self, session_id: str) -> int:
        count = self.session.switch_session(session_id)
        self.checkpoints.reload()
        self._apply_saved_model()
        self._clear_job_context()
        return count

    async def rename_session(self, name: str | None = None) -> str | None:
        return await self.session.rename(name)

    async def _maybe_compact(self) -> None:
        # When compaction actually shrinks the history, the checkpoints captured
        # against the old (absolute) indices are stale — rewinding to one would
        # slice the restructured history at the wrong boundary. Drop them so a
        # later rewind can't corrupt the conversation. (run_turn re-snapshots
        # after this, so the current turn keeps a valid rewind point.)
        if await self.session.maybe_compact():
            self.checkpoints.invalidate_after_compaction()

    async def _maybe_autoname(self) -> None:
        await self.session.maybe_autoname()

    async def _flush_resumable(self, captured, resumable) -> None:
        """Best-effort: repair any tool call the abort left unanswered and
        persist. Tolerates a slow disk with a short deadline so Ctrl-C remains
        snappy. Swallows ordinary failures (a flush failure must never mask the
        original exception) but lets a cancellation of the flush *itself*
        propagate. The caller re-raises whatever triggered the flush."""
        try:
            recovered = _repair_unanswered_tool_calls(
                _drop_nameless_tool_calls(list(captured) if captured else resumable)
            )
            self.session.history = recovered
            await asyncio.wait_for(
                asyncio.to_thread(self.session.persist),
                timeout=0.25,
            )
        except asyncio.CancelledError:
            # A second Ctrl-C (or shutdown) cancelled the flush itself. Don't
            # swallow it — propagate so teardown stays snappy rather than
            # dropping the shutdown signal on the floor.
            logger.debug("resumable flush cancelled", exc_info=True)
            raise
        except Exception:
            # Ordinary failure or the 0.25s deadline (asyncio.TimeoutError is an
            # Exception). Best-effort: never mask the original exception.
            logger.debug("resumable flush failed or timed out", exc_info=True)

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
        if self.session.summarizer is not None:
            self.session.summarizer = make_summarizer(model)
        if self.session.titler is not None:
            self.session.titler = make_titler(model)
        if persist:
            self.session.set_model(model_id)

    @property
    def mode(self) -> Mode:
        """The current approval mode (auto/ask/plan)."""
        return self.deps.mode

    def set_mode(self, mode: Mode) -> None:
        """Set the approval mode. The single write point for ``deps.mode`` so the
        interface layer doesn't poke ``harness.deps`` field-by-field."""
        self.deps.mode = mode

    def cycle_mode(self) -> Mode:
        """Advance to the next approval mode and return it."""
        self.deps.mode = self.deps.mode.cycle()
        return self.deps.mode

    def _apply_saved_model(self) -> None:
        """Re-point at a session's saved model after loading it, if one differs
        from what's already active."""
        if (
            self.session.store is not None
            and self.session.store.model
            and self.model_source is not None
            and self.session.store.model != self.model_id
        ):
            self.set_model(self.session.store.model, persist=False)

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
        self.mcp.disable_server(name, self.deps.workspace_root)

    async def enable_server(self, name: str) -> str | None:
        return await self.mcp.enable_server(name, self.deps.workspace_root)

    # --- hooks (observe-only except session_start, which injects context into
    # the next turn; dispatch + payload assembly live on ``self.hooks``) ---

    async def session_start(self, source: str) -> None:
        """Fire the SessionStart hook (``source`` is ``startup``/``resume``/
        ``clear``) and stash any returned context for the next turn's prompt."""
        ctx = await self.hooks.session_start(source)
        if ctx:
            self._pending_hook_context = ctx

    async def session_end(self, reason: str = "exit") -> None:
        """Fire the SessionEnd hook on teardown. Observe-only."""
        await self.hooks.session_end(reason)

    async def _assemble_prompt(self, typed: str) -> str:
        """Build the turn's prompt from what the user ``typed``, prepending any
        pending context — a finished-jobs digest, the prior turn's actionable
        error note, SessionStart-injected context, and UserPromptSubmit hook
        output — then wrapping the injected prefix in the turn-context envelope
        so a resumed session can recover just the typed text. The one-shot notes
        and the digest are consumed here."""
        prompt = typed
        # Current task checklist as turn-state (not consumed) — see
        # render_checklist_block for why it rides in the per-turn envelope rather
        # than the (cache-stable) system prompt.
        checklist = render_checklist_block(self.deps.tasks.items)
        if checklist:
            prompt = f"{checklist}\n\n{prompt}"
        # The finished-jobs digest. Prefer one re-stashed by a previously-failed
        # turn (so it isn't lost); otherwise drain the live buffer. Draining
        # clears deps.jobs's finished-since-turn state, so a turn that then fails
        # would forget it — we capture what we consumed (below) and the failure
        # path re-stashes it so the next turn re-emits it.
        digest = self._pending_jobs_digest or self.deps.jobs.take_finished_digest()
        self._pending_jobs_digest = None
        if digest:
            prompt = f"{digest}\n\n{prompt}"
        # Surface the prior turn's actionable failure (if any) once, so the model
        # can correct course rather than blindly retrying. Consumed here. Not
        # re-stashed on failure: the new failure overwrites it with its own note.
        if self._pending_error_note:
            prompt = f"{self._pending_error_note}\n\n{prompt}"
            self._pending_error_note = None
        # Prepend any SessionStart-injected context, once.
        hook_context = self._pending_hook_context
        if self._pending_hook_context:
            prompt = f"{self._pending_hook_context}\n\n{prompt}"
            self._pending_hook_context = None
        # Record the one-shot consumables (hook context + jobs digest) for this
        # turn so the run-failure path in run_turn can restore them — they're only
        # truly "delivered" if the run reaches the model successfully. The error
        # note is deliberately excluded (a fresh failure replaces it).
        self._consumed_this_turn = (hook_context, digest or None)
        # Fire UserPromptSubmit and prepend any context it returns.
        ctx = await self.hooks.user_prompt_submit(prompt)
        if ctx:
            prompt = f"{ctx}\n\n{prompt}"
        # If anything was injected above, wrap it in the turn-context envelope so
        # a resumed session can recover just the typed text. The injected blocks
        # are the prefix; `typed` is the unchanged suffix, sliced back out here.
        if prompt != typed:
            # Every prepend above follows `f"{block}\n\n{prompt}"`, so `typed` is
            # always an intact suffix and the injected prefix is recoverable by
            # length. Guard the invariant: if a future prepend ever breaks it, the
            # silent alternative is shipping a corrupted prompt to the model.
            assert prompt.endswith(typed), "turn-context injection must keep `typed` as a suffix"
            injected = prompt[: len(prompt) - len(typed)].rstrip("\n")
            prompt = wrap_turn_context(injected, typed)
        return prompt

    def steer(self, text: str,
              attachments: list[tuple[bytes, str]] | None = None) -> None:
        """Inject a user message into the running turn. Reaches the model at the
        next request boundary (pydantic-ai drains 'asap' content before it).
        Buffers if no run is live yet; the buffer flushes when a ctx is captured."""
        self._steer_buffer.append((text, attachments))
        self._flush_steers()

    def _flush_steers(self) -> None:
        if self._active_run_ctx is None or not self._steer_buffer:
            return
        for text, atts in self._steer_buffer:
            self._active_run_ctx.enqueue(
                text,
                *(BinaryContent(data=d, media_type=m) for d, m in (atts or [])),
                priority="asap",
            )
        self._steer_buffer = []

    def take_buffered_steers(
        self,
    ) -> list[tuple[str, list[tuple[bytes, str]] | None]]:
        """Return and clear any steers that were never flushed (the
        finishing-gap race). The caller decides what to do with them."""
        buffered, self._steer_buffer = self._steer_buffer, []
        return buffered

    def _build_hooked_handler(self, base_handler):
        """Wrap the event-stream handler to (1) capture the live RunContext for
        steering and (2) fire Pre/PostToolUse hooks on tool events. Returns
        ``None`` when there's neither a base handler nor hooks, so headless runs
        don't stream just to capture a ctx nobody steers."""
        if base_handler is None and self.deps.hooks is None:
            return None
        _call_inputs: dict = {}

        async def _wrapped(stream_ctx, events):
            # Capture the live RunContext so steer() can enqueue onto it. Set on
            # every streamed node, so it stays current within the run.
            self._active_run_ctx = stream_ctx
            self._flush_steers()  # deliver any steers buffered before this ctx

            async def _relay():
                async for event in events:
                    if self.deps.hooks is not None:
                        await self.hooks.tool_event(event, _call_inputs)
                    yield event

            if base_handler is not None:
                await base_handler(stream_ctx, _relay())
            else:
                async for _ in _relay():
                    pass

        return _wrapped

    async def _run_with_approval(
        self,
        user_prompt,
        deferred_results,
        toolsets,
        event_stream_handler,
        resumable: list[ModelMessage],
    ) -> str:
        """Drive the agent.run loop, handling DeferredToolRequests approval rounds,
        persisting on success, and rolling back to ``resumable`` on interrupt.
        Returns the final text output."""
        # The token estimate gating compaction is a coarse char/4 heuristic, so it
        # can undershoot the real window and let a too-large request reach the
        # provider. If the provider rejects it for length, force a compaction and
        # retry the run once (this flag latches so we never loop on it).
        overflow_retried = False
        while True:
            # capture_run_messages exposes the messages exchanged even when the
            # run aborts (a render error in the event handler, an API failure,
            # the user cancelling). Each agent.run gets its own context — the
            # capture only tracks the first run within a context, and this loop
            # may run several rounds. On failure we persist what was captured so
            # the user's prompt survives and the session can continue, rather
            # than discarding the turn entirely.
            # A per-round usage accumulator that pydantic-ai mutates in place as
            # each model step completes. Passing it in (rather than reading only
            # the returned result.usage) is what lets a turn that dies mid-run
            # still bank the tokens it already burned: on the success path the
            # returned result.usage IS this object, and on the failure path it
            # holds the partial usage from any steps that finished before the
            # error. Fresh per round so the success-path `+= result.usage` below
            # counts each round exactly once.
            round_usage = RunUsage()
            with capture_run_messages() as captured:
                try:
                    result = await self.agent.run(
                        user_prompt,
                        model=self.current_model,
                        message_history=self.session.history,
                        deps=self.deps,
                        deferred_tool_results=deferred_results,
                        event_stream_handler=event_stream_handler,
                        toolsets=toolsets,
                        usage=round_usage,
                    )
                except BaseException as exc:
                    # Bank whatever the failed run already spent. The provider
                    # billed those tokens regardless of the abort, so dropping
                    # them would make the session's running total undercount. A
                    # pure in-memory add — safe even on the cancel teardown path
                    # (it can't block the re-raise / Ctrl-C). Counts the failed
                    # attempt on the overflow-retry path too: those tokens were
                    # spent before the compaction-and-retry below.
                    self.session.usage += round_usage
                    # Context-overflow recovery: the request exceeded the real
                    # window despite our estimate. Force a compaction and retry the
                    # run once. Only when the compaction actually shrank the history
                    # (else a retry would just fail identically). The compacted
                    # history is persisted by maybe_compact, so it also becomes the
                    # rollback baseline for the retry.
                    if (
                        not overflow_retried
                        and is_context_overflow_error(exc)
                        and await self.session.maybe_compact(force=True)
                    ):
                        overflow_retried = True
                        resumable = list(self.session.history)
                        continue
                    # Persist what survives the failure so the user's prompt and
                    # any completed work aren't lost, repairing any tool call the
                    # abort left unanswered (the captured messages may stop right
                    # after one) so the session stays resumable. Fall back to the
                    # last clean history if the run produced nothing. The flush
                    # runs with a tight deadline so a slow disk (or Ctrl-C during
                    # a hung write) doesn't block the re-raise — the session is
                    # best-effort by design.
                    await self._flush_resumable(captured, resumable)
                    # Stash an actionable note (None for infra/render/cancel) to
                    # prepend to the next turn's prompt.
                    self._pending_error_note = _actionable_error_note(exc)
                    # Spill the full provider payload to disk so the real upstream
                    # error survives the terse on-screen view. Best-effort and
                    # deadline-bounded (like the flush above) so a slow disk on
                    # the teardown path can't block the re-raise / Ctrl-C. A
                    # cancellation here propagates rather than being swallowed.
                    try:
                        await asyncio.wait_for(
                            asyncio.to_thread(
                                dump_provider_error, self.deps.workspace_root, exc
                            ),
                            timeout=0.25,
                        )
                    except Exception:
                        logger.debug("failed to dump provider error", exc_info=True)
                    raise
            # This round's streaming ends the moment run() returns, so the
            # captured ctx is now stale. Null it before the approval modal /
            # next-round gap so a steer arriving in that window buffers and is
            # delivered to the next round's fresh ctx, rather than being enqueued
            # onto a completed RunContext.
            self._active_run_ctx = None
            # The run reached the model and returned, so this turn's one-shot
            # consumables (hook context / jobs digest) were genuinely delivered.
            # Clear the restore-on-failure stash so a later approval-round failure
            # doesn't re-emit context the model already saw. Idempotent across
            # rounds — only the first success matters.
            self._consumed_this_turn = (None, None)
            self.session.history = result.all_messages()
            self.session.usage += result.usage
            if isinstance(result.output, DeferredToolRequests):
                # This history ends with unanswered tool calls; keep it in memory
                # for the continuation run but do NOT persist it. A cancel or
                # failure during approval would otherwise leave the session
                # ending in a dangling tool_use — unresumable. Roll back to the
                # last clean state if the approval round is interrupted.
                if self.deps.mode is Mode.ask and result.output.approvals:
                    names = ", ".join(
                        getattr(c, "tool_name", None) or "(unknown)"
                        for c in result.output.approvals
                    )
                    await self.hooks.notification(
                        "approval_needed", "Approval needed", names
                    )
                try:
                    deferred_results = await resolve_approvals(
                        result.output, self.deps.mode, self.deps.request_approval
                    )
                except BaseException:
                    self.session.history = resumable
                    self.session.persist()
                    raise
                user_prompt = None  # continuation is driven by deferred_results
                continue
            self.session.persist()
            # This round completed cleanly and is persisted — it becomes the new
            # rollback baseline for any subsequent round.
            resumable = list(self.session.history)
            # Compact after the turn completes so the gauge never shows >100%
            # for long: the mid-turn growth is folded in immediately rather
            # than waiting for the next turn's start-of-turn check.
            await self._maybe_compact()
            output = result.output
            await self.hooks.stop()
            await self._maybe_autoname()
            return output

    async def run_turn(self, prompt: str, event_stream_handler=None,
                       attachments: list[tuple[bytes, str]] | None = None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        await self._maybe_compact()
        # Capture a rewind point for this turn before any work runs. Remember where
        # the history stood and which checkpoint this is, so a turn that fails
        # without producing any model output can roll its (dead) checkpoint back.
        pre_turn_len = len(self.session.history)
        checkpoint_index = self.checkpoints.snapshot(prompt)
        user_prompt: str | list[str | BinaryContent] | None = await self._assemble_prompt(prompt)
        if attachments and user_prompt is not None:
            user_prompt = [user_prompt, *(BinaryContent(data=d, media_type=m)
                                          for d, m in attachments)]
        # Offer only the live servers that aren't disabled — a server muted at
        # runtime stays connected but its tools are withheld from the model.
        toolsets = self.mcp.live_toolsets()
        # When hooks are configured, intercept each streamed tool event to fire
        # Pre/PostToolUse, then forward to the original handler (or drain if none).
        event_stream_handler = self._build_hooked_handler(event_stream_handler)
        # Self-heal a session left mid-exchange by an earlier aborted turn or a
        # flaky model. Two distinct malformations both make every provider reject
        # the next request and wedge the session: a nameless ToolCallPart (a
        # partial tool call whose function name never streamed) and a ToolCallPart
        # with no matching return ("unprocessed tool calls"). Strip the former,
        # then repair the latter, before running so the session resumes instead.
        sanitized = _drop_nameless_tool_calls(self.session.history)
        repaired = _repair_unanswered_tool_calls(sanitized)
        if repaired is not self.session.history:
            self.session.history = repaired
            self.session.persist()
        # The last persisted, resumable history — guaranteed free of unanswered
        # tool calls. Captured once here and refreshed only after a clean
        # persist; the deferred-approval round below deliberately holds a dirty
        # history in self.session.history, so this must NOT be recomputed from it
        # per iteration (that poisoned the rollback baseline across rounds).
        resumable = list(self.session.history)
        try:
            return await self._run_with_approval(
                user_prompt, deferred_results=None, toolsets=toolsets,
                event_stream_handler=event_stream_handler, resumable=resumable,
            )
        except BaseException:
            # The run never reached the model (it failed before the first round
            # returned, so _run_with_approval left _consumed_this_turn set). The
            # one-shot consumables we drained at assembly — SessionStart hook
            # context and the finished-jobs digest — would otherwise be lost
            # forever; re-stash them so the next turn re-emits them. After a
            # successful round the stash is already cleared, so a later
            # approval-round failure restores nothing (the model already saw it).
            hook_context, jobs_digest = self._consumed_this_turn
            self._consumed_this_turn = (None, None)
            if hook_context and not self._pending_hook_context:
                self._pending_hook_context = hook_context
            if jobs_digest and not self._pending_jobs_digest:
                self._pending_jobs_digest = jobs_digest
            # Roll back this turn's start-of-turn checkpoint if the turn failed
            # before producing any model response (the resumable flush has already
            # run by now, so the history reflects what survived). Such a checkpoint
            # is a dead rewind target — its preview is just the failed prompt and it
            # points right before a turn that did nothing — and a string of failed
            # retries would otherwise litter /rewind with them. The bare prompt
            # still persists for resumability; only the useless checkpoint goes.
            if not _turn_produced_response(self.session.history, pre_turn_len):
                self.checkpoints.discard(checkpoint_index)
            raise
        finally:
            self._active_run_ctx = None
