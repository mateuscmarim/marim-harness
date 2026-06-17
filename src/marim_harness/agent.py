from typing import Optional

from pydantic_ai import Agent, DeferredToolRequests, capture_run_messages
from pydantic_ai.messages import FunctionToolCallEvent, FunctionToolResultEvent

from .workspace import (
    discover_agents,
    effective_tools,
    find_agent,
    subagent_instructions,
)
from .compaction import (
    Summarizer,
    Titler,
    make_summarizer,
    make_titler,
)
from .deps import Deps
from .hooks import events as hook_events
from .hooks.runner import base_payload
from .instructions import register_instructions
from .mcp import McpManager
from .permissions import Mode, resolve_approvals
from .session import SessionController, SessionManager, SessionStore
from .tools.provider import ToolProvider

# Envelope wrapped around any context injected into a turn's prompt — job
# digests, error notes, and SessionStart/UserPromptSubmit hook output. It is
# prepended to what the user typed, so the typed text stays the suffix. The
# envelope gives that boundary a stable marker so a resumed session can show
# only what the user typed (matching the live TUI, which mounts the typed text
# before injection happens). Plain turns carry no envelope and are unchanged.
_TURN_CONTEXT_OPEN = "<turn-context>"
_TURN_CONTEXT_CLOSE = "</turn-context>"
_TURN_CONTEXT_SEP = f"{_TURN_CONTEXT_CLOSE}\n\n"


def wrap_turn_context(injected: str, typed: str) -> str:
    """Wrap ``injected`` context in the turn-context envelope and append the
    user's ``typed`` prompt after it. Inverse of :func:`strip_turn_context`."""
    return f"{_TURN_CONTEXT_OPEN}\n{injected}\n{_TURN_CONTEXT_SEP}{typed}"


def strip_turn_context(content: str) -> str:
    """Return only the user-typed portion of a persisted prompt, dropping any
    leading turn-context envelope that :meth:`Harness.run_turn` prepended. A
    prompt with no envelope is returned unchanged."""
    if not content.startswith(_TURN_CONTEXT_OPEN):
        return content
    idx = content.find(_TURN_CONTEXT_SEP)
    if idx == -1:
        return content
    return content[idx + len(_TURN_CONTEXT_SEP):]


def _has_unanswered_tool_calls(history: list) -> bool:
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


def _short(exc: BaseException, limit: int = 200) -> str:
    """A whitespace-collapsed, length-capped rendering of an exception — never a
    traceback, just the one-line gist that's safe to hand back to the model."""
    text = " ".join(str(exc).split())
    return text[: limit - 1] + "…" if len(text) > limit else text


def _actionable_error_note(exc: BaseException) -> Optional[str]:
    """A terse, sanitized note about a failed turn that the *model* can act on,
    or None when the failure is not the model's to fix. We surface only the
    errors where adjusting the next turn could plausibly help — a malformed or
    oversized request, a usage limit, the model failing to produce a usable
    response — and stay silent on harness/render bugs, cancellations, and
    transient infra (rate limits, 5xx), where a note would only mislead."""
    from pydantic_ai.exceptions import (
        ModelHTTPError,
        UnexpectedModelBehavior,
        UsageLimitExceeded,
    )

    head = "Note: your previous turn did not complete."
    if isinstance(exc, ModelHTTPError):
        # Client errors (context too long, malformed request) are the model's to
        # fix; rate limits (429) and server errors (5xx) are transient infra that
        # retrying — not re-prompting — should handle.
        if 400 <= exc.status_code < 500 and exc.status_code != 429:
            return (
                f"{head} The request was rejected (HTTP {exc.status_code}). "
                "Adjust your approach — e.g. shorten the input or fix the "
                "request — before continuing."
            )
        return None
    if isinstance(exc, UsageLimitExceeded):
        return (
            f"{head} A usage limit was reached ({_short(exc)}). Be more "
            "economical with tool calls and continue."
        )
    if isinstance(exc, UnexpectedModelBehavior):
        return f"{head} {_short(exc)}. Adjust your approach and continue."
    return None


class Harness:
    """Owns the Pydantic AI agent and drives one user turn to completion,
    resolving deferred tool approvals by the current mode."""

    def __init__(self, model, provider: ToolProvider, deps: Deps, instructions: str,
                 model_label: str = "model", store: Optional[SessionStore] = None,
                 manager: Optional[SessionManager] = None,
                 max_context_tokens: int = 100_000, keep_last_messages: int = 20,
                 summarizer: Optional[Summarizer] = None,
                 titler: Optional[Titler] = None, model_source=None,
                 model_id: Optional[str] = None, proactive_memory: bool = False,
                 mcp_servers=None, mcp_disabled=None):
        self.agent = Agent(
            model,
            deps_type=Deps,
            instructions=instructions,
            output_type=[str, DeferredToolRequests],
            # One extra retry past pydantic-ai's default of 1: weaker models
            # often need a second attempt to correct a malformed tool argument
            # before the turn fails with UnexpectedModelBehavior.
            retries=2,
        )
        self.provider = provider
        provider.register(self.agent)
        self.mcp = McpManager(mcp_servers or [], set(mcp_disabled or []))
        register_instructions(self.agent, self.mcp, proactive_memory)
        self.deps = deps
        # The spawn_agent tool reaches the runners through Deps, the same way
        # other tools reach shared state. Wired here so they track model switches.
        self.deps.run_subagent = self._run_subagent
        self.deps.run_background_agent = self._run_background_subagent
        self.model_label = model_label
        # The model object used for each turn (swappable at runtime), the source
        # that builds new ones, and the id of the active model.
        self.current_model = model
        self.model_source = model_source
        self.model_id = model_id
        # A one-shot note about the last actionable failure, prepended to the
        # next turn's prompt so the model knows it didn't complete (see
        # _actionable_error_note). None when there's nothing to surface.
        self._pending_error_note: Optional[str] = None
        # One-shot context returned by a SessionStart hook, prepended to the next
        # turn's prompt and consumed there (mirrors _pending_error_note).
        self._pending_hook_context: Optional[str] = None
        self.session = SessionController(
            store, manager, deps,
            max_context_tokens, keep_last_messages,
            summarizer, titler,
        )

    # --- session lifecycle (operations carrying harness-level logic; plain
    # state and persistence live on ``self.session`` and are reached directly) ---

    def resume(self) -> int:
        count = self.session.resume()
        self._apply_saved_model()
        return count

    def reset(self) -> None:
        self.session.reset()

    def new_session(self, name: Optional[str] = None) -> None:
        self.session.new_session(name)
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
        self._apply_saved_model()
        return count

    async def rename_session(self, name: Optional[str] = None) -> Optional[str]:
        return await self.session.rename(name)

    async def _maybe_compact(self) -> None:
        await self.session.maybe_compact()

    async def _maybe_autoname(self) -> None:
        await self.session.maybe_autoname()

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

    def _subagent_handler(self, stream_id: str):
        """An event_stream_handler for a sub-agent run that forwards each event to
        the UI, tagged with ``stream_id`` so it can stream nested under the spawn.
        None when no UI is listening (headless) — the run just doesn't stream."""
        cb = self.deps.on_subagent_event
        if cb is None:
            return None

        async def handler(ctx, events) -> None:
            async for event in events:
                tokens = getattr(getattr(ctx, "usage", None), "total_tokens", 0) or 0
                await cb(stream_id, event, tokens)

        return handler

    def _build_subagent(self, type: str):
        """Build an isolated sub-agent of ``type`` on the current model, with its
        reach decided up front: gated tools only in auto mode, so a run never
        needs an approval round. Returns ``(agent, None)`` or, for an unknown
        type, ``(None, message)`` listing what's available."""
        defn = find_agent(self.deps.workspace_root, type)
        if defn is None:
            names = ", ".join(a.name for a in discover_agents(self.deps.workspace_root))
            return None, f"No sub-agent type {type!r}. Available: {names}."
        allow_gated = self.deps.mode is Mode.auto
        sub = Agent(
            self.current_model,
            deps_type=Deps,
            instructions=subagent_instructions(defn, self.deps.workspace_root),
        )
        self.provider.register_subagent(sub, effective_tools(defn, allow_gated=allow_gated))
        return sub, None

    async def _run_subagent(
        self, type: str, task: str, stream_id: str, mcp_names: list[str] | None = None
    ) -> str:
        """Spawn one isolated sub-agent of ``type``, run it to completion on
        ``task``, and return its final report — streaming its events to the UI
        nested under the spawn. Shares the workspace Deps (read-only use) but
        starts a fresh conversation, so the sub-agent gets a clean context.
        ``mcp_names`` is the MCP servers the main agent granted this spawn (none
        by default); granted servers gate via the same approval hook as the main
        agent's."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self.mcp.granted_servers(mcp_names)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_START,
                self._hook_payload(hook_events.SUBAGENT_START, subagent_type=type, task=task),
            )
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_STOP,
                self._hook_payload(
                    hook_events.SUBAGENT_STOP, subagent_type=type, task=task,
                    result=result.output,
                ),
            )
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.session.usage += result.usage
        return self.mcp.grant_note(unknown) + result.output

    async def _run_background_subagent(
        self, type: str, task: str, mcp_names: list[str] | None = None
    ) -> str:
        """Run a sub-agent as a detached background job: same isolation, mode-based
        reach, and MCP grant as a foreground spawn, but with no event streaming —
        the job's result is its final report, surfaced when the agent pulls it.
        Any unknown-server note rides along on that report."""
        sub, err = self._build_subagent(type)
        if err is not None:
            return err
        granted, unknown = self.mcp.granted_servers(mcp_names)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_START,
                self._hook_payload(hook_events.SUBAGENT_START, subagent_type=type, task=task),
            )
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        if self.deps.hooks is not None:
            await self.deps.hooks.dispatch(
                hook_events.SUBAGENT_STOP,
                self._hook_payload(
                    hook_events.SUBAGENT_STOP, subagent_type=type, task=task,
                    result=result.output,
                ),
            )
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.session.usage += result.usage
        self.session.persist()
        return self.mcp.grant_note(unknown) + result.output

    # --- MCP lifecycle (connection control; server state and grant resolution
    # live on ``self.mcp`` and are reached directly) ---

    async def connect(self) -> dict:
        return await self.mcp.connect()

    async def aclose(self) -> None:
        await self.mcp.aclose()

    async def disable_server(self, name: str) -> None:
        self.mcp.disable_server(name, self.deps.workspace_root)

    async def enable_server(self, name: str) -> Optional[str]:
        return await self.mcp.enable_server(name, self.deps.workspace_root)

    def _hook_payload(self, event: str, **extra) -> dict:
        """Build a hook payload with the common fields drawn from the live
        session, plus any event-specific extras."""
        store = self.session.store
        return base_payload(
            event,
            session_id=store.session_id if store is not None else "",
            cwd=str(self.deps.workspace_root),
            transcript_path=str(store.path) if store is not None else "",
            **extra,
        )

    async def session_start(self, source: str) -> None:
        """Fire the SessionStart hook (``source`` is ``startup``/``resume``/
        ``clear``) and stash any returned context for the next turn's prompt."""
        if self.deps.hooks is None:
            return
        ctx = await self.deps.hooks.dispatch(
            hook_events.SESSION_START,
            self._hook_payload(hook_events.SESSION_START, source=source),
        )
        if ctx:
            self._pending_hook_context = ctx

    async def session_end(self, reason: str = "exit") -> None:
        """Fire the SessionEnd hook on teardown. Observe-only."""
        if self.deps.hooks is None:
            return
        await self.deps.hooks.dispatch(
            hook_events.SESSION_END,
            self._hook_payload(hook_events.SESSION_END, reason=reason),
        )

    async def _fire_tool_event(self, event, _call_inputs: dict | None = None) -> None:
        """Map a streamed tool event to a Pre/PostToolUse hook (observe-only).

        ``_call_inputs`` is a per-turn dict (tool_call_id → tool_input) used to
        correlate a PostToolUse result with the args from its matching call, so
        that CC plugin scripts receive ``tool_input`` on both event types.
        """
        if self.deps.hooks is None:
            return
        if isinstance(event, FunctionToolCallEvent):
            try:
                tool_input = event.part.args_as_dict()
            except Exception:
                tool_input = {}
            # Stash input so the paired PostToolUse event can include it.
            if _call_inputs is not None:
                _call_inputs[event.part.tool_call_id] = tool_input
            await self.deps.hooks.dispatch(
                hook_events.PRE_TOOL_USE,
                self._hook_payload(
                    hook_events.PRE_TOOL_USE,
                    tool_name=event.part.tool_name,
                    tool_input=tool_input,
                ),
            )
        elif isinstance(event, FunctionToolResultEvent):
            # Look up the stashed input by tool_call_id; fall back gracefully.
            tool_input = ({} if _call_inputs is None
                          else _call_inputs.get(event.tool_call_id, {}))
            await self.deps.hooks.dispatch(
                hook_events.POST_TOOL_USE,
                self._hook_payload(
                    hook_events.POST_TOOL_USE,
                    tool_name=getattr(event.part, "tool_name", ""),
                    tool_input=tool_input,
                    tool_response=str(getattr(event.part, "content", "")),
                ),
            )

    async def run_turn(self, prompt: str, event_stream_handler=None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        await self._maybe_compact()
        typed = prompt  # what the user actually typed; stays the prompt's suffix
        digest = self.deps.jobs.take_finished_digest()
        if digest:
            prompt = f"{digest}\n\n{prompt}"
        # Surface the prior turn's actionable failure (if any) once, so the model
        # can correct course rather than blindly retrying. Consumed here.
        if self._pending_error_note:
            prompt = f"{self._pending_error_note}\n\n{prompt}"
            self._pending_error_note = None
        # Prepend any SessionStart-injected context, once.
        if self._pending_hook_context:
            prompt = f"{self._pending_hook_context}\n\n{prompt}"
            self._pending_hook_context = None
        # Fire UserPromptSubmit and prepend any context it returns.
        if self.deps.hooks is not None:
            ctx = await self.deps.hooks.dispatch(
                hook_events.USER_PROMPT_SUBMIT,
                self._hook_payload(hook_events.USER_PROMPT_SUBMIT, prompt=prompt),
            )
            if ctx:
                prompt = f"{ctx}\n\n{prompt}"
        # If anything was injected above, wrap it in the turn-context envelope so
        # a resumed session can recover just the typed text. The injected blocks
        # are the prefix; `typed` is the unchanged suffix, sliced back out here.
        if prompt != typed:
            injected = prompt[: len(prompt) - len(typed)].rstrip("\n")
            prompt = wrap_turn_context(injected, typed)
        user_prompt: Optional[str] = prompt
        deferred_results = None
        # Offer only the live servers that aren't disabled — a server muted at
        # runtime stays connected but its tools are withheld from the model.
        toolsets = self.mcp.live_toolsets()
        # When hooks are configured, intercept each streamed tool event to fire
        # Pre/PostToolUse, then forward to the original handler (or drain if none).
        if self.deps.hooks is not None:
            _base_handler = event_stream_handler
            # Scoped to this single turn: maps tool_call_id → tool_input so the
            # PostToolUse branch can include the call's args in its payload.
            _call_inputs: dict = {}

            async def _hooked_handler(stream_ctx, events):
                async def _relay():
                    async for event in events:
                        await self._fire_tool_event(event, _call_inputs)
                        yield event

                if _base_handler is not None:
                    await _base_handler(stream_ctx, _relay())
                else:
                    async for _ in _relay():
                        pass

            event_stream_handler = _hooked_handler
        while True:
            # The last persisted, resumable history — what we fall back to if
            # this round is interrupted before it completes cleanly.
            clean_history = list(self.session.history)
            # capture_run_messages exposes the messages exchanged even when the
            # run aborts (a render error in the event handler, an API failure,
            # the user cancelling). Each agent.run gets its own context — the
            # capture only tracks the first run within a context, and this loop
            # may run several rounds. On failure we persist what was captured so
            # the user's prompt survives and the session can continue, rather
            # than discarding the turn entirely.
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
                    )
                except BaseException as exc:
                    # Persist what survives the failure — but never a history
                    # ending in an unanswered tool call (the captured messages
                    # may stop right after one). That would corrupt the session,
                    # so fall back to the last clean state in that case.
                    recovered = list(captured) if captured else clean_history
                    if _has_unanswered_tool_calls(recovered):
                        recovered = clean_history
                    self.session.history = recovered
                    self.session.persist()
                    # Stash an actionable note (None for infra/render/cancel) to
                    # prepend to the next turn's prompt.
                    self._pending_error_note = _actionable_error_note(exc)
                    raise
            self.session.history = result.all_messages()
            self.session.usage += result.usage
            if isinstance(result.output, DeferredToolRequests):
                # This history ends with unanswered tool calls; keep it in memory
                # for the continuation run but do NOT persist it. A cancel or
                # failure during approval would otherwise leave the session
                # ending in a dangling tool_use — unresumable. Roll back to the
                # last clean state if the approval round is interrupted.
                try:
                    deferred_results = await resolve_approvals(
                        result.output, self.deps.mode, self.deps.request_approval
                    )
                except BaseException:
                    self.session.history = clean_history
                    self.session.persist()
                    raise
                user_prompt = None  # continuation is driven by deferred_results
                continue
            self.session.persist()
            output = result.output
            if self.deps.hooks is not None:
                await self.deps.hooks.dispatch(
                    hook_events.STOP, self._hook_payload(hook_events.STOP)
                )
            await self._maybe_autoname()
            return output
