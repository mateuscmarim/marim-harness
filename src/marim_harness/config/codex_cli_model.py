"""Run the OpenAI Codex CLI (``codex app-server``) as a main-loop model provider.

A ChatGPT/Codex subscription is reachable only through the ``codex`` CLI, which
runs its own agentic loop. Like claude-cli, this provider makes marim a
*launcher*: one marim turn becomes one ``turn/start`` on a Codex thread that
lives for the marim session, Codex runs its tools inside its own sandbox, and
marim receives a single **text-only** ``ModelResponse``. Emitting
``ToolCallPart``s here would make pydantic_ai's agent graph try to execute
Codex's tool calls a second time, so Codex's activity is either pushed
out-of-band as tool cards (``on_activity`` bound by the TUI) or folded into
the streamed text as ``▸`` lines (headless).

Unlike claude-cli, Codex *asks marim* before privileged actions: those server
requests are answered by ``codex.approvals.ApprovalBroker`` through the same
``request_approval``/``ask_user`` seams native tools use, so ask mode gates
per call and plan mode is read-only + never-prompt (spec §Mode mapping).

Threads: the thread id is persisted with the marim session (Task 10,
``SessionStore.cli_thread_id``) through ``on_session_ref``/``session_ref_getter``
as ``"codex-cli:<thread id>"``; a resumed marim session first tries
``thread/resume`` and, if Codex no longer has the thread, starts a fresh one
seeded with the flattened history (the same cold-start rule claude-cli uses).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models import ModelRequestParameters, StreamedResponse
from pydantic_ai.usage import RequestUsage

from ..codex.approvals import ApprovalBroker, UiSeams, policy_for, sandbox_for, sandbox_mode_for
from ..codex.collab import ChildSinks, ChildStreams, CollabRouter, LedgerOnly, Routed, router_for
from ..codex.env import INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.quota import QuotaHint, quota_from
from ..codex.server import (
    CodexServer,
    ThreadHandle,
    ThreadOptions,
    TurnOptions,
    close_shared_server_if_idle,
    is_shared_server,
    shared_server,
)
from ..codex.transcript import activity_events
from ..codex.translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta
from ..codex.turn import TurnState, finish_turn, turn_events
from ..runtime.permissions import Mode
from .cli_input import attachment_content, codex_input, extract_system, prompt_content
from .context_report import CONTEXT_REPORT_KEY, ContextReport
from .external_cli import (
    CLI_ACTIVITY_KEY,
    ActivityLedger,
    CliModelError,
    ExternalCliModel,
    TextFolder,
)

if TYPE_CHECKING:
    from collections.abc import AsyncGenerator, Awaitable, Callable

    from pydantic_ai.settings import ModelSettings

logger = logging.getLogger(__name__)

__all__ = ["CliModelError", "CodexCliModel", "CodexStreamedResponse", "effort_for"]

SESSION_REF_PREFIX = "codex-cli:"

# marim thinking level -> Codex reasoning effort (spec §Thinking -> effort).
_EFFORT_FOR_LEVEL = {"minimal": "low", "low": "low", "medium": "medium", "high": "high"}


def effort_for(level: str | None, supported: list[str] | None) -> str | None:
    """``turn/start.effort`` for a marim thinking level. ``off``/None omit the
    key (Codex's own default). ``xhigh`` is sent only when the model lists it;
    otherwise (or when the catalog is unknown) it degrades to ``high``."""
    if not level or level == "off":
        return None
    if level == "xhigh":
        return "xhigh" if supported and "xhigh" in supported else "high"
    return _EFFORT_FOR_LEVEL.get(level, "medium")


def fold_activity_text(item: object, leading: bool) -> str:
    """Headless rendering: a tool call becomes a ``▸ name(args)`` line; results
    are folded only when they failed (so a headless transcript stays short but
    a failing command is visible)."""
    if isinstance(item, ActivityStart):
        args = item.args
        arg = args.get("command") or args.get("path") or args.get("query") or args.get("task") or ""
        line = f"▸ {item.tool_name} {arg}".rstrip()
    elif isinstance(item, ActivityEnd) and item.is_error:
        first = item.content.strip().splitlines()[:1]
        line = f"▸ failed: {first[0] if first else 'error'}"
    else:
        return ""
    return line + "\n" if leading else "\n" + line + "\n"


class CodexCliModel(ExternalCliModel):
    """A Pydantic AI model backed by ``codex app-server``. See the module
    docstring for the shape; the late-bound seams are on ``ExternalCliModel``."""

    provider_id = "codex-cli"

    def __init__(
        self,
        model_id: str | None,
        *,
        ephemeral: bool = False,
        server: CodexServer | None = None,
    ) -> None:
        super().__init__()
        # An empty id (`MARIM_MODEL=` set but blank) means "Codex's own
        # default" exactly like None does; sent verbatim it reaches the
        # app-server as `model: ""`, which Codex rejects with 400 "The ''
        # model is not supported" (seen on the PR #128 live probe).
        self._model_id = model_id or None
        self.ephemeral = ephemeral
        # Injected in tests; production models share the process-wide server
        # (one `codex app-server` per marim process, spec §Supervisor).
        self._server = server
        # Two facts, kept apart because they diverge for an ephemeral clone.
        # `_injected`: the caller chose the binary, so the ambient PATH/login
        # probe is skipped (see _ensure_server) and `self._server` is pinned;
        # a NON-injected model instead follows the process-wide singleton (see
        # `server`). `_owns_server`: aclose() may close a non-singleton
        # server outright — true for every model except a clone, which only
        # borrows its parent's server (shared or private) and must never
        # close it out from under the parent's session.
        self._injected = server is not None
        self._owns_server = True
        # Clones made by ephemeral_clone(); closed with their parent, because
        # nothing else holds them (Harness.aclose knows only the session's
        # current model, the aux titler/summarizer/advisor keep theirs inside
        # a pydantic-ai Agent). An aux clone keeps ONE long-lived thread, and
        # a thread nobody drops pins the shared app-server open for good —
        # close_shared_server_if_idle could never fire.
        self._clones: list[CodexCliModel] = []
        self.thread: ThreadHandle | None = None
        # The latest quota reading (`account/rateLimits/read`), refreshed
        # once per turn; the TUI status bar renders it. None until the first
        # turn completes, or whenever the read fails.
        self.quota_hint: QuotaHint | None = None
        # The latest context reading (`thread/tokenUsage/updated`: the last
        # response's prompt size against the model window), refreshed with
        # every update while a turn streams; the status bar / GET session
        # prefer it over marim's estimate. None until the first update.
        self.context_report: ContextReport | None = None
        # Per-model catalog of reasoning efforts (model id -> efforts), filled
        # lazily from model/list on the first turn; drives effort_for.
        self._efforts: dict[str, list[str]] | None = None
        self._broker: ApprovalBroker | None = None
        # Codex-side sub-agents (``codex/collab.py``): one router per live
        # thread — it outlives a turn because the children do (Codex keeps a
        # spawned agent around for later ``wait``/``sendInput`` calls).
        self._router: CollabRouter | None = None

    # --- identity -------------------------------------------------------------
    @property
    def model_name(self) -> str:
        return self._model_id or "default"

    def ephemeral_clone(self, *, cwd: str) -> CodexCliModel:
        """A stateless, read-only copy for one-shot aux agents (titler/
        summarizer): a fresh ephemeral Codex thread per request, plan mode so
        it can't edit, no session ref so it can never resume — or hijack — the
        user's live thread."""
        clone = CodexCliModel(self._model_id, ephemeral=True, server=self._server)
        # The clone borrows the parent's server (shared or privately injected)
        # — its aclose() must drop only its own thread, never close a server
        # the parent's session is still running on. It also inherits whether
        # that server was injected: a clone of a shared-server parent must
        # keep probing availability like its parent (a logged-out aux agent
        # should fail with the actionable message, not a bare spawn error).
        clone._injected = self._injected
        clone._owns_server = False
        self._clones.append(clone)
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        return clone

    @property
    def server(self) -> CodexServer:
        # A non-injected model follows the singleton. If that was reset out
        # from under us (another harness's aclose found it momentarily idle,
        # or an explicit close_shared_server), re-resolve it rather than
        # restart the stale object: restarting would spawn a second
        # `codex app-server` that no aclose path would ever reach again.
        if self._server is None or (not self._injected and not is_shared_server(self._server)):
            self._server = shared_server()
        return self._server

    async def aclose(self) -> None:
        """A private server (a test, or an embedder wiring its own
        ``CodexServer`` in directly) is owned by the model it was injected
        into and closed unconditionally there. Anything else — the
        process-wide shared singleton, or an ephemeral clone borrowing its
        parent's private server — may still be serving other harnesses/
        spawns/clones in the same process (a daemon holds many concurrently);
        closing it out from under them would sever every other live thread,
        so this only drops THIS model's own thread and, for the singleton,
        lets ``close_shared_server_if_idle`` close it once nothing else is
        registered on it (final review Important #4). Clones go first — a
        clone may have resolved a server before its parent ever ran, and
        its thread counts against the singleton's idleness."""
        for clone in self._clones:
            await clone.aclose()
        self._clones.clear()
        server = self._server
        if server is None:
            return
        if is_shared_server(server):
            self._drop_own_thread(server)
            await close_shared_server_if_idle()
        elif self._owns_server:
            await server.aclose()
            self._router = None
        else:
            self._drop_own_thread(server)

    def release_conversation(self) -> None:
        """Drop the live thread handle (see the base): the next turn then
        ``thread/resume``s whatever the rebound store names, or starts a new
        thread. The server stays — it is process-wide and serves every other
        session on it. The context report goes with the thread it described."""
        if self._server is not None:
            self._drop_own_thread(self._server)
        self.context_report = None

    def _drop_own_thread(self, server: CodexServer) -> None:
        if self.thread is not None:
            server.drop_thread(self.thread)  # cascades to the adopted children
            self.thread = None
        self._router = None

    # --- mode / policy ----------------------------------------------------------
    def _mode(self) -> Mode:
        raw = self.mode_getter() if self.mode_getter is not None else "plan"
        try:
            return Mode(raw)
        except ValueError:
            return Mode.plan

    def _thinking(self, model_settings: ModelSettings | None) -> str | None:
        level = (model_settings or {}).get("thinking")
        if level is None and self.thinking_getter is not None:
            level = self.thinking_getter()
        return str(level) if level else None

    def _scratchpad(self) -> Path | None:
        return self.scratchpad_getter() if self.scratchpad_getter is not None else None

    def _make_broker(self) -> ApprovalBroker:
        return ApprovalBroker(
            mode_getter=self._mode,
            workspace_root=Path(self.cwd),
            scratchpad_getter=self._scratchpad,
            ui=UiSeams(request_approval=self.request_approval, ask_user=self.ask_user),
        )

    # --- thread lifecycle -------------------------------------------------------
    async def _ensure_server(self) -> CodexServer:
        # The environment-wide availability probe (binary on PATH + logged in)
        # only makes sense against the real, globally-resolved CLI. A caller
        # that injected its own server (tests, and any future explicit wiring)
        # already knows which binary it wants to run and how to reach it —
        # `server.start()` below raises its own CodexUnavailable for THAT
        # binary if it's missing, so gating on the global probe too would
        # reject a perfectly good injected server just because "codex" isn't
        # on the ambient PATH. See task-8-report.md for the brief deviation.
        # The probe re-runs whenever the (non-injected) server would have to
        # be (re)spawned, not just on the very first turn: a `codex logout`
        # or uninstall between turns must surface as the actionable
        # "unavailable" error, not as a bare spawn failure.
        server = self.server
        if not self._injected and not server.alive and not codex_available():
            raise CliModelError(f"codex CLI unavailable. {INSTALL_HINT}")
        try:
            await server.start()
        except CodexUnavailable as exc:
            raise CliModelError(f"codex app-server failed to start: {exc}") from exc
        return server

    async def _load_efforts(self, server: CodexServer) -> None:
        if self._efforts is not None:
            return
        try:
            models = await server.list_models()
        except Exception as exc:  # best-effort catalog; effort degrades to `high`
            logger.debug("codex model/list failed: %s", exc)
            self._efforts = {}
            return
        self._efforts = {
            str(m.get("id") or m.get("model")): [
                str(e.get("reasoningEffort") or e) for e in m.get("supportedReasoningEfforts") or []
            ]
            for m in models
        }

    def _persisted_thread_id(self) -> str | None:
        if self.ephemeral or self.session_ref_getter is None:
            return None
        ref = self.session_ref_getter()
        if not ref or not ref.startswith(SESSION_REF_PREFIX):
            return None  # another provider's ref (e.g. claude-cli) — ignore
        return ref[len(SESSION_REF_PREFIX) :] or None

    async def _thread_for(self, messages: list, server: CodexServer) -> tuple[ThreadHandle, bool]:
        """The thread to run this turn on, and whether it is FRESH (the first
        input must carry the flattened history). Order: the live handle; a
        ``thread/resume`` of the persisted id; a new thread."""
        if self.thread is not None and server.alive and self.thread.thread_id in server.thread_ids:
            return self.thread, False
        mode = self._mode()
        self._broker = self._make_broker()
        options = ThreadOptions(
            cwd=self.cwd,
            developer_instructions=extract_system(messages) or None,
            model=self._model_id,
            ephemeral=self.ephemeral,
            sandbox=sandbox_mode_for(mode),
            approval_policy=policy_for(mode),
        )
        request_handler = self._broker.handle
        persisted = self._persisted_thread_id()
        if persisted is not None:
            handle = await server.resume_thread(
                persisted, options=options, request_handler=request_handler
            )
            if handle is not None:
                self._own_thread(server, handle)
                return handle, False
            logger.info("codex thread %s gone; starting fresh with flattened history", persisted)
        handle = await server.start_thread(options=options, request_handler=request_handler)
        self._own_thread(server, handle)
        if not self.ephemeral and self.on_session_ref is not None:
            self.on_session_ref(SESSION_REF_PREFIX + handle.thread_id)
        # FRESH (full flattened history needed) whenever `messages` carries more
        # than the current request — a resumed marim session, a mid-session
        # switch to codex-cli (SessionController.set_model clears a foreign
        # provider's ref, so `persisted` is None here even though history
        # exists), or a resume whose thread Codex no longer has. Mirrors
        # claude_cli_model.py's cold-start rule. A genuinely first-ever turn has
        # only the current request in `messages`, so flattening it would just
        # reformat the same single prompt — skip that to keep behavior
        # unchanged for the common case.
        return handle, len(messages) > 1

    def _own_thread(self, server: CodexServer, handle: ThreadHandle) -> None:
        """Take ``handle`` as this model's thread, with a fresh collab router
        (its children register under the handle) whose labels the broker
        uses to prefix a child's approval prompts."""
        self.thread = handle
        self._router = router_for(server, handle)
        if self.job_registry is not None and not self.ephemeral:
            from ..runtime.backend_jobs import CodexJobObserver

            handle.on_observation = CodexJobObserver(
                self.job_registry, handle.thread_id, self.on_jobs_settled
            )
        if self._broker is not None:
            self._broker.label_for = self._router.label_for

    def _child_sinks(self) -> ChildSinks:
        return ChildSinks(
            on_event=self.on_subagent,
            on_model=self.on_subagent_model,
            on_notice=self.on_subagent_notice,
            on_usage=self.on_subagent_usage,
        )

    async def _begin_turn(
        self, messages: list, model_settings: ModelSettings | None
    ) -> tuple[CodexServer, ThreadHandle, str]:
        server = await self._ensure_server()
        await self._load_efforts(server)
        handle, fresh = await self._thread_for(messages, server)
        inputs = codex_input(prompt_content(messages, history=fresh))
        logger.debug(
            "codex turn input: images=%d, replay_history=%s",
            sum(item["type"] == "image" for item in inputs),
            fresh,
        )
        mode = self._mode()
        supported = (self._efforts or {}).get(self._model_id or "", None)
        turn_id = await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=inputs,
                model=self._model_id,
                effort=effort_for(self._thinking(model_settings), supported),
                approval_policy=policy_for(mode),
                sandbox_policy=sandbox_for(mode, self.cwd),
            ),
        )
        return server, handle, turn_id

    # --- Model API ----------------------------------------------------------------
    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        server, handle, turn_id = await self._begin_turn(messages, model_settings)
        state = self._turn_state()
        parts: list[str] = []
        try:
            async for item in turn_events(server, handle, state, turn_id=turn_id):
                if isinstance(item, TextDelta):
                    parts.append(item.delta)
                elif isinstance(item, (ActivityStart, ActivityEnd)):
                    seg = fold_activity_text(item, leading=not parts)
                    if seg:
                        parts.append(seg)
                elif isinstance(item, Notice):
                    logger.info("codex: %s", item.message)
                # Routed child traffic / LedgerOnly: nothing to fold — a
                # child's spawn card is the `▸ spawn_agent` line above.
        finally:
            self._seal_children()
        await self._refresh_quota(server)
        usage = finish_turn(handle, state)
        return ModelResponse(
            parts=[TextPart(content="".join(parts))],
            model_name=self.model_name,
            timestamp=datetime.now(tz=timezone.utc),
            usage=usage,
            provider_name="codex-cli",
            provider_details=self._response_details(),
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        server, handle, turn_id = await self._begin_turn(messages, model_settings)
        state = self._turn_state()
        stream = CodexStreamedResponse(
            model_request_parameters=model_request_parameters,
            _items=turn_events(server, handle, state, turn_id=turn_id),
            _after=lambda: self._refresh_quota(server),
            _finish=lambda: finish_turn(handle, state),
            _details=self._response_details,
            _model_id=self.model_name,
            _ts=datetime.now(tz=timezone.utc),
            _on_activity=self.on_activity,
            _children=ChildStreams(self._child_sinks()),
            _seal=self._seal_children,
        )
        try:
            yield stream
        finally:
            if handle.current_turn_id is not None:  # abandoned mid-turn
                with contextlib.suppress(Exception):
                    await server.interrupt(handle)
                handle.current_turn_id = None

    def _seal_children(self) -> list[LedgerOnly]:
        """End of a turn: the ledger-only returns for the children still
        running (see ``CollabRouter.seal_open``)."""
        return self._router.seal_open() if self._router is not None else []

    # --- context / quota ----------------------------------------------------------------
    def _turn_state(self) -> TurnState:
        """A turn's accumulator, publishing each usage update's context
        reading onto ``context_report`` as it streams. Seeded with the last
        report so a window learned on an earlier turn survives an update
        that omits ``modelContextWindow``."""
        return TurnState(
            context=self.context_report, on_context=self._note_context, router=self._router
        )

    def _note_context(self, report: ContextReport) -> None:
        self.context_report = report

    def _response_details(self) -> dict | None:
        """``provider_details`` for the turn's response: the context report,
        persisted so a resumed session shows Codex's last known numbers
        before its first new turn (``context_report.last_context_report``)."""
        if self.context_report is None:
            return None
        return {CONTEXT_REPORT_KEY: self.context_report.to_payload()}

    async def _refresh_quota(self, server: CodexServer) -> None:
        """Poll ``account/rateLimits/read`` once, after a turn's events have
        drained (so it runs on the consuming task's normal await path, never
        inside a cancellation-time ``finally``), and keep the reading for the
        status bar. Failures are ignored: the hint is informational, and a
        server that died mid-turn already raised through ``turn_events``."""
        try:
            self.quota_hint = quota_from(await server.read_rate_limits())
        except Exception as exc:  # noqa: BLE001 - best-effort status-line hint
            logger.debug("codex account/rateLimits/read failed: %s", exc)
            # Clear rather than keep the previous reading: the status bar
            # renders any hint it has, and a stale number is a false claim.
            self.quota_hint = None

    # --- live controls ---------------------------------------------------------------
    def steer(self, text: str, attachments: list[tuple[bytes, str]] | None = None) -> bool:
        """Forward a mid-turn steer to ``turn/steer``. Fire-and-forget on the
        running loop: the harness calls this synchronously from the input
        path. Returns False (harness keeps buffering) when no turn is live."""
        handle = self.thread
        if handle is None or handle.current_turn_id is None or self._server is None:
            return False
        server = self._server
        loop = asyncio.get_running_loop()
        inputs = codex_input(attachment_content(text, attachments))
        task = loop.create_task(server.steer(handle, text, inputs=inputs))
        task.add_done_callback(lambda t: t.exception() if not t.cancelled() else None)
        return True

    async def compact_remote(self) -> None:
        if self.thread is None or self._server is None:
            return
        await self._server.compact(self.thread)


@dataclass
class CodexStreamedResponse(StreamedResponse):
    """Streams the turn's translated items as text-delta events (prose) and
    out-of-band tool cards (``_on_activity``), folding ``▸`` lines instead
    when no UI is bound — the same two rendering modes as claude-cli."""

    _items: AsyncIterator[object] | None = None
    # Awaited once the items drain, before _finish: the model's per-turn
    # quota poll. Best-effort — it never raises into the stream.
    _after: Callable[[], Awaitable[None]] | None = None
    _finish: Callable[[], RequestUsage] | None = None
    # The provider_details to attach at settle (the context report), merged
    # next to the activity ledger.
    _details: Callable[[], dict | None] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None
    # Codex-side sub-agents: where a child's routed traffic goes, and the
    # end-of-turn seal for the cards still open (``codex/collab.py``).
    _children: ChildStreams | None = None
    _seal: Callable[[], list[LedgerOnly]] | None = None

    async def _get_event_iterator(self):
        if self._items is None:
            return
        ledger = ActivityLedger(self._attach_activity)
        folder = TextFolder(
            self._parts_manager,
            ledger.recording(self._on_activity),
            activity_events=activity_events,
            fold_text=fold_activity_text,
            is_call=lambda item: isinstance(item, ActivityStart),
        )
        # Ledger-only spawn entries (a child's seal/resume) matter only in
        # cards mode: fold mode's ▸ lines are its own persisted record.
        note = ledger.note_activity if self._on_activity is not None else (lambda events: None)
        try:
            async for item in self._items:
                if isinstance(item, Routed):
                    if self._children is not None:
                        await self._children.deliver(item)
                elif isinstance(item, LedgerOnly):
                    note(activity_events(item.item))
                else:
                    async for ev in self._events_for(item, folder):
                        ledger.note_event(ev)
                        yield ev
        finally:
            # Also on an interrupt: the partial response's ledger must not
            # end with a spawn_agent call nobody answered.
            for sealed in self._seal() if self._seal is not None else ():
                note(activity_events(sealed.item))
        await self._settle()

    async def _events_for(self, item: object, folder: TextFolder):
        """The pydantic-ai events for one translated item."""
        if isinstance(item, TextDelta):
            async for ev in folder.emit_text(item.delta):
                yield ev
        elif isinstance(item, ThinkingDelta):
            for ev in self._parts_manager.handle_thinking_delta(
                vendor_part_id=f"think-{item.item_id}", content=item.delta
            ):
                yield ev
        elif isinstance(item, (ActivityStart, ActivityEnd)):
            async for ev in folder.emit_tool(item):
                yield ev

    def _attach_activity(self, entries: list[dict]) -> None:
        """The ledger's first tool entry: expose it on the response so it
        survives into ``get()`` (an interrupted stream's partial one too)."""
        self.provider_details = {**(self.provider_details or {}), CLI_ACTIVITY_KEY: entries}

    async def _settle(self) -> None:
        """Runs once the items drain: fold the turn's usage and mark the
        response finished FIRST, then the per-turn quota poll — it awaits,
        and a cancellation landing in that await must not cost the turn its
        usage. The poll is best-effort and never raises into the stream."""
        if self._finish is not None:
            self._usage = self._finish()
        details = self._details() if self._details is not None else None
        if details:
            self.provider_details = {**(self.provider_details or {}), **details}
        self._finished = True
        if self._after is not None:
            await self._after()

    @property
    def model_name(self) -> str:
        return self._model_id

    @property
    def timestamp(self) -> datetime:
        return self._ts or datetime.now(tz=timezone.utc)

    @property
    def provider_name(self) -> str:
        return "codex-cli"

    @property
    def provider_url(self) -> str:
        return "https://openai.com/codex"
