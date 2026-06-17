from typing import Optional

from pydantic_ai import Agent, DeferredToolRequests, capture_run_messages
from pydantic_ai.usage import RunUsage

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
from .instructions import register_instructions
from .mcp import McpManager
from .permissions import Mode, resolve_approvals
from .session import SessionController, SessionInfo, SessionManager, SessionStore
from .tools.provider import ToolProvider


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
        self.session = SessionController(
            store, manager, deps,
            max_context_tokens, keep_last_messages,
            summarizer, titler,
        )

    # --- session delegation ---

    @property
    def history(self) -> list:
        return self.session.history

    @history.setter
    def history(self, value: list) -> None:
        self.session.history = value

    @property
    def usage(self) -> RunUsage:
        return self.session.usage

    @usage.setter
    def usage(self, value: RunUsage) -> None:
        self.session.usage = value

    @property
    def store(self):
        return self.session.store

    @store.setter
    def store(self, value) -> None:
        self.session.store = value

    @property
    def manager(self):
        return self.session.manager

    @property
    def summarizer(self):
        return self.session.summarizer

    @summarizer.setter
    def summarizer(self, value) -> None:
        self.session.summarizer = value

    @property
    def titler(self):
        return self.session.titler

    @titler.setter
    def titler(self, value) -> None:
        self.session.titler = value

    @property
    def on_compact(self):
        return self.session.on_compact

    @on_compact.setter
    def on_compact(self, value) -> None:
        self.session.on_compact = value

    @property
    def on_rename(self):
        return self.session.on_rename

    @on_rename.setter
    def on_rename(self, value) -> None:
        self.session.on_rename = value

    @property
    def max_context_tokens(self) -> int:
        return self.session.max_context_tokens

    @max_context_tokens.setter
    def max_context_tokens(self, value: int) -> None:
        self.session.max_context_tokens = value

    @property
    def keep_last_messages(self) -> int:
        return self.session.keep_last_messages

    @keep_last_messages.setter
    def keep_last_messages(self, value: int) -> None:
        self.session.keep_last_messages = value

    @property
    def total_tokens(self) -> int:
        return self.session.usage.total_tokens

    @property
    def session_name(self) -> Optional[str]:
        return self.session.session_name

    def sessions(self) -> list[SessionInfo]:
        return self.session.sessions()

    def _persist(self) -> None:
        self.session.persist()

    def resume(self) -> int:
        count = self.session.resume()
        self._apply_saved_model()
        return count

    def reset(self) -> None:
        self.session.reset()

    def new_session(self, name: Optional[str] = None) -> None:
        self.session.new_session(name, model_id=self.model_id)

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
            self.store is not None
            and self.store.model
            and self.model_source is not None
            and self.store.model != self.model_id
        ):
            self.set_model(self.store.model, persist=False)

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
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(
            task, deps=self.deps, toolsets=granted,
            event_stream_handler=self._subagent_handler(stream_id),
        )
        # A foreground spawn runs inside the current turn, so its spend is folded
        # into the session total here and persisted by run_turn's _persist.
        self.session.usage += result.usage
        return self._mcp_grant_note(unknown) + result.output

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
        granted, unknown = self._granted_servers(mcp_names)
        result = await sub.run(task, deps=self.deps, toolsets=granted)
        # A background spawn finishes off-turn, so no run_turn will fold in its
        # spend — count it here and persist right away so the saved session
        # reflects it even if the process exits before the next turn.
        self.session.usage += result.usage
        self.session.persist()
        return self._mcp_grant_note(unknown) + result.output

    # --- MCP delegation ---

    @property
    def mcp_servers(self) -> list:
        return self.mcp.mcp_servers

    @property
    def _live_servers(self) -> list:
        return self.mcp._live_servers

    @_live_servers.setter
    def _live_servers(self, value: list) -> None:
        self.mcp._live_servers = value

    @property
    def disabled(self) -> set:
        return self.mcp.disabled

    @disabled.setter
    def disabled(self, value: set) -> None:
        self.mcp.disabled = value

    @property
    def mcp_status(self) -> dict:
        return self.mcp.mcp_status

    def _server_name(self, server) -> str:
        return McpManager.server_name(server)

    def configured_names(self) -> list[str]:
        return self.mcp.configured_names()

    def _enabled_server_names(self) -> list[str]:
        return self.mcp.enabled_names()

    def mcp_index_text(self) -> str:
        return self.mcp.mcp_index_text()

    def _granted_servers(self, names: list[str] | None) -> tuple[list, list[str]]:
        return self.mcp.granted_servers(names)

    def _mcp_grant_note(self, unknown: list[str]) -> str:
        return self.mcp.grant_note(unknown)

    async def connect(self) -> dict:
        return await self.mcp.connect()

    async def aclose(self) -> None:
        await self.mcp.aclose()

    async def disable_server(self, name: str) -> None:
        self.mcp.disable_server(name, self.deps.workspace_root)

    async def enable_server(self, name: str) -> Optional[str]:
        return await self.mcp.enable_server(name, self.deps.workspace_root)

    async def run_turn(self, prompt: str, event_stream_handler=None) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        await self._maybe_compact()
        digest = self.deps.jobs.take_finished_digest()
        if digest:
            prompt = f"{digest}\n\n{prompt}"
        # Surface the prior turn's actionable failure (if any) once, so the model
        # can correct course rather than blindly retrying. Consumed here.
        if self._pending_error_note:
            prompt = f"{self._pending_error_note}\n\n{prompt}"
            self._pending_error_note = None
        user_prompt: Optional[str] = prompt
        deferred_results = None
        # Offer only the live servers that aren't disabled — a server muted at
        # runtime stays connected but its tools are withheld from the model.
        toolsets = self.mcp.live_toolsets()
        while True:
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
                        message_history=self.history,
                        deps=self.deps,
                        deferred_tool_results=deferred_results,
                        event_stream_handler=event_stream_handler,
                        toolsets=toolsets,
                    )
                except BaseException as exc:
                    if captured:
                        self.session.history = list(captured)
                        self.session.persist()
                    # Stash an actionable note (None for infra/render/cancel) to
                    # prepend to the next turn's prompt.
                    self._pending_error_note = _actionable_error_note(exc)
                    raise
            self.session.history = result.all_messages()
            self.session.usage += result.usage
            self.session.persist()
            if isinstance(result.output, DeferredToolRequests):
                deferred_results = await resolve_approvals(
                    result.output, self.deps.mode, self.deps.request_approval
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
            output = result.output
            await self._maybe_autoname()
            return output
