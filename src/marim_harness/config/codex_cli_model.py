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
from ..codex.env import INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.server import CodexServer, ThreadHandle, ThreadOptions, TurnOptions, shared_server
from ..codex.translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta
from ..codex.turn import TurnState, finish_turn, text_input, turn_events
from ..runtime.permissions import Mode
from .claude_cli_model import extract_system, flatten_history, latest_user_text
from .external_cli import CliModelError, ExternalCliModel, TextFolder

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


# --- activity rendering (shared with the spawn path, Task 11) ----------------
# `item` takes `ActivityStart | ActivityEnd` in practice, but stays annotated
# as `object` here so these match `TextFolder.__init__`'s callback shape
# (`Callable[[object], ...]`, shared with claude-cli's untyped equivalents) —
# a narrower parameter type is a real contravariance mismatch pyright flags.
def activity_events(item: object) -> list:
    """pydantic-ai tool events for a Codex activity, for the TUI card sinks
    (``on_activity``) — the same shapes ``cli_activity_events`` builds for
    claude-cli. Never enters the ModelResponse."""
    from pydantic_ai.messages import (
        FunctionToolCallEvent,
        FunctionToolResultEvent,
        ToolCallPart,
        ToolReturnPart,
    )

    if isinstance(item, ActivityStart):
        part = ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
        return [FunctionToolCallEvent(part=part)]
    assert isinstance(item, ActivityEnd)
    part = ToolReturnPart(
        tool_name="tool",
        content=item.content,
        tool_call_id=item.item_id,
        timestamp=datetime.now(tz=timezone.utc),
        outcome="failed" if item.is_error else "success",
    )
    return [FunctionToolResultEvent(part=part)]


def fold_activity_text(item: object, leading: bool) -> str:
    """Headless rendering: a tool call becomes a ``▸ name(args)`` line; results
    are folded only when they failed (so a headless transcript stays short but
    a failing command is visible)."""
    if isinstance(item, ActivityStart):
        arg = item.args.get("command") or item.args.get("path") or item.args.get("query") or ""
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
        self._model_id = model_id
        self.ephemeral = ephemeral
        # Injected in tests; production models share the process-wide server
        # (one `codex app-server` per marim process, spec §Supervisor).
        self._server = server
        self.thread: ThreadHandle | None = None
        # Per-model catalog of reasoning efforts (model id -> efforts), filled
        # lazily from model/list on the first turn; drives effort_for.
        self._efforts: dict[str, list[str]] | None = None
        self._broker: ApprovalBroker | None = None

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
        clone.cwd = cwd
        clone.mode_getter = lambda: "plan"
        return clone

    @property
    def server(self) -> CodexServer:
        if self._server is None:
            self._server = shared_server()
        return self._server

    async def aclose(self) -> None:
        """Tests own their server; production closes the shared one via
        ``Harness.aclose`` -> ``close_shared_server`` (Task 10)."""
        if self._server is not None:
            await self._server.aclose()

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
        if self._server is None and not codex_available():
            raise CliModelError(f"codex CLI unavailable. {INSTALL_HINT}")
        server = self.server
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
                self.thread = handle
                return handle, False
            logger.info("codex thread %s gone; starting fresh with flattened history", persisted)
        handle = await server.start_thread(options=options, request_handler=request_handler)
        self.thread = handle
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

    async def _begin_turn(
        self, messages: list, model_settings: ModelSettings | None
    ) -> tuple[CodexServer, ThreadHandle]:
        server = await self._ensure_server()
        await self._load_efforts(server)
        handle, fresh = await self._thread_for(messages, server)
        text = flatten_history(messages) if fresh else latest_user_text(messages)
        mode = self._mode()
        supported = (self._efforts or {}).get(self._model_id or "", None)
        handle.usage_baseline = dict(handle.usage_baseline)
        await server.start_turn(
            handle,
            options=TurnOptions(
                inputs=[text_input(text)],
                model=self._model_id,
                effort=effort_for(self._thinking(model_settings), supported),
                approval_policy=policy_for(mode),
                sandbox_policy=sandbox_for(mode, self.cwd),
            ),
        )
        return server, handle

    # --- Model API ----------------------------------------------------------------
    async def request(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
    ) -> ModelResponse:
        server, handle = await self._begin_turn(messages, model_settings)
        state = TurnState()
        parts: list[str] = []
        async for item in turn_events(server, handle, state):
            if isinstance(item, TextDelta):
                parts.append(item.delta)
            elif isinstance(item, (ActivityStart, ActivityEnd)):
                seg = fold_activity_text(item, leading=not parts)
                if seg:
                    parts.append(seg)
            elif isinstance(item, Notice):
                logger.info("codex: %s", item.message)
        usage = finish_turn(handle, state)
        return ModelResponse(
            parts=[TextPart(content="".join(parts))],
            model_name=self.model_name,
            timestamp=datetime.now(tz=timezone.utc),
            usage=usage,
            provider_name="codex-cli",
        )

    @asynccontextmanager
    async def request_stream(
        self,
        messages: list,
        model_settings: ModelSettings | None,
        model_request_parameters: ModelRequestParameters,
        run_context=None,
    ) -> AsyncGenerator[StreamedResponse]:
        server, handle = await self._begin_turn(messages, model_settings)
        state = TurnState()
        stream = CodexStreamedResponse(
            model_request_parameters=model_request_parameters,
            _items=turn_events(server, handle, state),
            _finish=lambda: finish_turn(handle, state),
            _model_id=self.model_name,
            _ts=datetime.now(tz=timezone.utc),
            _on_activity=self.on_activity,
        )
        try:
            yield stream
        finally:
            if handle.current_turn_id is not None:  # abandoned mid-turn
                with contextlib.suppress(Exception):
                    await server.interrupt(handle)
                handle.current_turn_id = None

    # --- live controls ---------------------------------------------------------------
    def steer(self, text: str) -> bool:
        """Forward a mid-turn steer to ``turn/steer``. Fire-and-forget on the
        running loop: the harness calls this synchronously from the input
        path. Returns False (harness keeps buffering) when no turn is live."""
        handle = self.thread
        if handle is None or handle.current_turn_id is None or self._server is None:
            return False
        server = self._server
        loop = asyncio.get_running_loop()
        task = loop.create_task(server.steer(handle, text))
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
    _finish: Callable[[], RequestUsage] | None = None
    _model_id: str = "default"
    _ts: datetime | None = None
    _on_activity: Callable[[list], Awaitable[None]] | None = None

    async def _get_event_iterator(self):
        if self._items is None:
            return
        folder = TextFolder(
            self._parts_manager,
            self._on_activity,
            activity_events=activity_events,
            fold_text=fold_activity_text,
            is_call=lambda item: isinstance(item, ActivityStart),
        )
        async for item in self._items:
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
        if self._finish is not None:
            self._usage = self._finish()
        self._finished = True

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
