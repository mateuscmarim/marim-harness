"""``backend: codex-cli`` sub-agents: one Codex thread per spawn.

The spawn runs on the process-wide ``codex app-server`` (``codex.server``),
inside the same ``SpawnLifecycle`` the native and claude-cli paths use — hooks
bracketing, output cap/spill, worktree close, background persist are written
once, not per backend. What is codex-specific:

* **Reach is decided up front, never mid-run** (the native rule). The sandbox
  is ``read-only`` unless the agent's effective tools include a mutating tool
  (``GATED_TOOLS``: write_file/edit_file/bash), and plan mode is always
  read-only. Codex's ``approvalPolicy`` follows the parent's mode exactly like
  the main loop: auto → on-request, ask → untrusted (brokered to the parent's
  approval panel, labelled with the agent name), plan → never.
* **Structured output** rides Codex's native ``outputSchema`` (any root type),
  so ``resolve_output_schema`` hands the schema through untouched.
* **Resume** reopens the persisted thread (``codex_thread_id`` in the sidecar)
  with ``thread/resume`` and sends the continuation prompt as a new turn.

MCP grants are NOT forwarded (Codex has its own MCP config); a non-empty
``mcp_names`` is noted in the output, mirroring claude-cli.
"""

from __future__ import annotations

import logging
import os
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from ..codex.approvals import ApprovalBroker, UiSeams, policy_for, sandbox_for, sandbox_mode_for
from ..codex.env import CODEX_MODEL_ENV, INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.server import CodexServer, ThreadHandle, ThreadOptions, TurnOptions, shared_server
from ..codex.translate import ActivityEnd, ActivityStart, Notice, TextDelta, ThinkingDelta
from ..codex.turn import TurnState, finish_turn, text_input, turn_events
from ..config.codex_cli_model import activity_events, effort_for
from ..config.external_cli import CliModelError
from ..runtime.permissions import Mode
from ..thinking import resolve_thinking
from ..tools.names import GATED_TOOLS
from ..workspace import effective_tools
from .backend import CONTINUATION_PROMPT, SpawnLifecycle, SpawnRun
from .isolation import SpawnWorktree
from .persistence import SpawnTranscripts, count_tool_calls

if TYPE_CHECKING:
    from ..hooks.dispatch import TurnHooks
    from ..runtime.deps import Deps
    from ..workspace.agents import AgentDef

logger = logging.getLogger(__name__)

BACKEND = "codex-cli"


@dataclass
class CodexRun:
    output: str
    transcript: list[Any]
    usage: RunUsage
    thread_id: str | None


@dataclass
class _Transcript:
    """Folds translated Codex items into (a) pydantic-ai stream events for the
    sub-agents screen and (b) a pydantic-ai message list for the sidecar —
    the codex analog of ``cli_backend.CliStreamTranslator``. The spawn's
    *output* is the text of the LAST agent message (with ``outputSchema``
    that is the JSON document)."""

    messages: list[Any] = field(default_factory=list)
    _index: int = 0
    _texts: dict[str, list[str]] = field(default_factory=dict)
    _open_text: str | None = None  # item id of the TextPart being streamed
    _call_names: dict[str, str] = field(default_factory=dict)
    # The backing list for the in-progress ModelResponse's parts. `ModelResponse.parts`
    # is typed `Sequence[ModelResponsePart]` (pyright rejects `.append` on it), so the
    # mutable list lives here and is handed to ModelResponse by reference — same object,
    # so appending here is visible through `resp.parts` too.
    _parts: list[Any] = field(default_factory=list)

    def _response(self) -> ModelResponse:
        if self.messages and isinstance(self.messages[-1], ModelResponse):
            return self.messages[-1]
        self._parts = []
        resp = ModelResponse(parts=self._parts)
        self.messages.append(resp)
        return resp

    def feed(self, item: object) -> list[Any]:
        if isinstance(item, TextDelta):
            return self._text(item)
        if isinstance(item, ThinkingDelta):
            return self._thinking(item)
        if isinstance(item, ActivityStart):
            self._open_text = None
            self._call_names[item.item_id] = item.tool_name
            self._response()
            self._parts.append(
                ToolCallPart(tool_name=item.tool_name, args=item.args, tool_call_id=item.item_id)
            )
            return activity_events(item)
        if isinstance(item, ActivityEnd):
            self._open_text = None
            self.messages.append(
                ModelRequest(
                    parts=[
                        ToolReturnPart(
                            tool_name=self._call_names.get(item.item_id, "tool"),
                            content=item.content,
                            tool_call_id=item.item_id,
                            timestamp=datetime.now(tz=timezone.utc),
                        )
                    ]
                )
            )
            return activity_events(item)
        if isinstance(item, Notice):
            logger.info("codex spawn: %s", item.message)
        return []

    def _text(self, item: TextDelta) -> list[Any]:
        events: list[Any] = []
        self._response()
        if self._open_text != item.item_id:
            self._open_text = item.item_id
            self._index += 1
            self._parts.append(TextPart(content=""))
            events.append(PartStartEvent(index=self._index, part=TextPart(content="")))
        part = self._parts[-1]
        assert isinstance(part, TextPart)
        part.content += item.delta
        self._texts.setdefault(item.item_id, []).append(item.delta)
        events.append(
            PartDeltaEvent(index=self._index, delta=TextPartDelta(content_delta=item.delta))
        )
        return events

    def _thinking(self, item: ThinkingDelta) -> list[Any]:
        self._response()
        self._open_text = None
        last = self._parts[-1] if self._parts else None
        if isinstance(last, ThinkingPart):
            last.content += item.delta
            return [
                PartDeltaEvent(index=self._index, delta=ThinkingPartDelta(content_delta=item.delta))
            ]
        self._index += 1
        self._parts.append(ThinkingPart(content=item.delta))
        return [PartStartEvent(index=self._index, part=ThinkingPart(content=item.delta))]

    def output(self) -> str:
        if not self._texts:
            return ""
        last_id = next(reversed(self._texts))
        return "".join(self._texts[last_id])


@dataclass
class SpawnCollaborators:
    """The runner-owned collaborators a spawn orchestrator needs, grouped so
    ``CodexSpawnOrchestrator.__init__`` takes one value object instead of four
    loose keyword arguments (a ``PLR0913`` the ratchet gate flags). Mirrors
    what ``CliSpawnOrchestrator`` takes positionally — that one stays at
    exactly five arguments (its own ceiling), so it is left alone."""

    hooks: TurnHooks
    transcripts: SpawnTranscripts
    lifecycle: SpawnLifecycle
    resolve_agent: Callable[[str], AgentDef | None]


@dataclass
class CodexSpawnRequest:
    """What one ``run_codex`` call needs, bundled so the method takes one
    request object instead of eight loose values — the spawn-request analog
    of ``ThreadOptions``/``TurnOptions`` (``codex/server.py``)."""

    defn: AgentDef
    task: str
    work_root: Path | None
    model: str | None
    stream_id: str
    output_schema: dict | None = None
    thinking: str | None = None
    resume_thread_id: str | None = None
    # (messages, thread_id) -> None, called once the handle exists (well before
    # the first turn) and again as the transcript grows, so an interrupted
    # spawn's sidecar carries its `codex_thread_id` — see `execute`'s
    # `_checkpoint`. None when the caller (a direct `run_codex` test, or any
    # backend-agnostic caller) has no sidecar to checkpoint into.
    checkpoint: Callable[[list, str | None], None] | None = None


def _efforts_for(models: list[dict], model_id: str | None) -> list[str] | None:
    for m in models:
        if str(m.get("id") or m.get("model")) == model_id:
            efforts = m.get("supportedReasoningEfforts") or []
            return [str(e.get("reasoningEffort") or e) for e in efforts]
    return None


class CodexSpawnOrchestrator:
    """Execute/resume ``backend: codex-cli`` spawns. Same constructor shape as
    ``CliSpawnOrchestrator`` plus the inherited-thinking reader the native
    path has (``thinking_default``) and an injectable server for tests."""

    def __init__(
        self,
        *,
        deps: Deps,
        collaborators: SpawnCollaborators,
        thinking_default: Callable[[], str | None] | None = None,
        server: CodexServer | None = None,
    ) -> None:
        self.deps = deps
        self.hooks = collaborators.hooks
        self._transcripts = collaborators.transcripts
        self._lifecycle = collaborators.lifecycle
        self._resolve_agent = collaborators.resolve_agent
        self._thinking_default = thinking_default
        self._server = server

    # --- lifecycle wrapper (mirrors CliSpawnOrchestrator.execute) ---------------
    async def execute(
        self,
        defn: AgentDef,
        task: str,
        work_root: Path | None,
        iso: SpawnWorktree | None,
        mcp_names: list[str] | None,
        max_output_chars: int | None,
        model: str | None,
        stream_id: str,
        *,
        background: bool,
        resume_thread_id: str | None = None,
        original_task: str | None = None,
        depth: int = 1,
        transcript_prefix: list[Any] | None = None,
        output_schema: dict | None = None,
        thinking: str | None = None,
    ) -> str:
        hook_task = original_task or task
        t0 = time.perf_counter()
        meta: dict[str, Any] = {}
        checkpoint: Callable[[list, str | None], None] | None = None
        if stream_id:
            meta = {
                "stream_id": stream_id,
                "type": defn.name,
                "task": hook_task,
                "model": model,
                "mcp": None,
                "depth": depth,
                "max_output_chars": max_output_chars,
                "isolation": iso.branch if iso else None,
                "status": "running",
                "backend": BACKEND,
                "codex_thread_id": resume_thread_id,
            }

            def _checkpoint(messages: list, thread_id: str | None, _meta=meta) -> None:
                # Same shape as CliSpawnOrchestrator._checkpoint (cli_spawn.py):
                # patch the real thread id into the shared meta dict as soon as
                # it's known — right after start_thread/resume_thread returns,
                # well before the first turn — so a spawn cancelled before
                # completion is still resumable (Important #3 of the final
                # review). `_meta` is the same dict object `final_meta` below
                # spreads, so this mutation is visible there too.
                if thread_id:
                    _meta["codex_thread_id"] = thread_id
                self._transcripts.save(
                    stream_id, (transcript_prefix or []) + messages, meta=_meta, cap_reasoning=True
                )

            checkpoint = _checkpoint
            checkpoint([], None)
        await self.hooks.subagent_start(defn.name, hook_task)
        resumed = resume_thread_id is not None

        async def _run() -> SpawnRun:
            result = await self.run_codex(
                CodexSpawnRequest(
                    defn=defn,
                    task=task,
                    work_root=work_root,
                    model=model,
                    stream_id=stream_id,
                    output_schema=output_schema,
                    thinking=thinking,
                    resume_thread_id=resume_thread_id,
                    checkpoint=checkpoint,
                )
            )
            full_transcript = list(transcript_prefix or []) + result.transcript
            final_meta = {
                **meta,
                "status": "finished",
                "codex_thread_id": result.thread_id,
                "usage": {
                    "input": result.usage.input_tokens,
                    "output": result.usage.output_tokens,
                },
                "tool_count": count_tool_calls(full_transcript),
                "duration": time.perf_counter() - t0,
            }
            return SpawnRun(
                output=result.output,
                transcript=full_transcript,
                usage=result.usage,
                final_meta=final_meta,
                child_transcripts={},
            )

        return await self._lifecycle(
            _run,
            iso=iso,
            resumed=resumed,
            background=background,
            name=defn.name,
            stop_task=hook_task,
            note=self._mcp_note(mcp_names),
            max_output_chars=max_output_chars,
            stream_id=stream_id,
            timing=None,
        )

    @staticmethod
    def _mcp_note(mcp_names: list[str] | None) -> str:
        if not mcp_names:
            return ""
        names = ", ".join(mcp_names)
        return (
            f"[note: MCP servers ({names}) are not forwarded to codex-cli sub-agents; "
            "configure them in Codex's own config]\n\n"
        )

    # --- the codex run ---------------------------------------------------------------
    def _policy(self, defn: AgentDef) -> tuple[Mode, bool]:
        mode = self.deps.workspace.mode
        tools = effective_tools(
            defn, allow_gated=mode is Mode.auto, allow_net=mode is not Mode.plan
        )
        read_only = mode is Mode.plan or not (tools & GATED_TOOLS)
        return mode, read_only

    def _broker(self, mode: Mode, cwd: str, label: str) -> ApprovalBroker:
        services = getattr(self.deps, "services", None)
        get_scratchpad = getattr(services, "get_scratchpad", None)
        cbs = self.deps.ui
        return ApprovalBroker(
            mode_getter=lambda: mode,
            workspace_root=Path(cwd),
            scratchpad_getter=get_scratchpad or (lambda: None),
            ui=UiSeams(request_approval=cbs.request_approval, ask_user=cbs.ask_user),
            label=label,
        )

    async def _server_or_raise(self) -> CodexServer:
        if not codex_available():
            raise CliModelError(f"codex CLI unavailable. {INSTALL_HINT}")
        server = self._server if self._server is not None else shared_server()
        try:
            await server.start()
        except CodexUnavailable as exc:
            raise CliModelError(f"codex app-server failed to start: {exc}") from exc
        return server

    async def _acquire_thread(
        self,
        server: CodexServer,
        request: CodexSpawnRequest,
        options: ThreadOptions,
        broker: ApprovalBroker,
    ) -> ThreadHandle:
        """Start a fresh thread or resume a persisted one, then checkpoint the
        real thread id immediately — well before ``turn/start`` — so an
        interruption anywhere from here on (including before the first turn
        even starts) leaves a resumable sidecar (Important #3 of the final
        review)."""
        if request.resume_thread_id is not None:
            handle = await server.resume_thread(
                request.resume_thread_id, options=options, request_handler=broker.handle
            )
            if handle is None:
                raise CliModelError(
                    f"codex thread {request.resume_thread_id} is gone; cannot resume"
                )
        else:
            handle = await server.start_thread(options=options, request_handler=broker.handle)
        if request.checkpoint is not None:
            request.checkpoint([], handle.thread_id)
        return handle

    async def run_codex(self, request: CodexSpawnRequest) -> CodexRun:
        defn = request.defn
        stream_id = request.stream_id
        server = await self._server_or_raise()
        mode, read_only = self._policy(defn)
        cwd = str(request.work_root or self.deps.workspace.root)
        model_name = request.model or defn.model or os.environ.get(CODEX_MODEL_ENV) or None
        inherited = self._thinking_default() if self._thinking_default is not None else None
        level = resolve_thinking(request.thinking, defn.thinking, inherited)
        broker = self._broker(mode, cwd, defn.name)
        options = ThreadOptions(
            cwd=cwd,
            developer_instructions=defn.prompt,
            model=model_name,
            sandbox=sandbox_mode_for(mode, read_only=read_only),
            approval_policy=policy_for(mode),
        )
        handle = await self._acquire_thread(server, request, options, broker)
        cbs = self.deps.ui
        if stream_id and cbs.on_subagent_model is not None:
            await cbs.on_subagent_model(stream_id, f"{BACKEND}:{model_name or 'default'}")
        try:
            models = await server.list_models()
        except Exception:  # best-effort: effort degrades to `high` for xhigh
            models = []
        state = TurnState()
        tx = _Transcript()
        last_ckpt_len = 0
        try:
            turn_id = await server.start_turn(
                handle,
                options=TurnOptions(
                    inputs=[text_input(request.task)],
                    model=model_name,
                    effort=effort_for(level, _efforts_for(models, model_name)),
                    approval_policy=policy_for(mode),
                    sandbox_policy=sandbox_for(mode, cwd, read_only=read_only),
                    output_schema=request.output_schema,
                ),
            )
            async for item in turn_events(server, handle, state, turn_id=turn_id):
                events = tx.feed(item)
                # Checkpoint whenever the transcript grows (mirrors
                # cli_backend._consume's growth-gated checkpoint) so a
                # cancellation mid-turn loses at most the segment since the
                # last item, not the whole run.
                if request.checkpoint is not None and len(tx.messages) != last_ckpt_len:
                    last_ckpt_len = len(tx.messages)
                    request.checkpoint(tx.messages, None)
                for event in events:
                    if stream_id and cbs.on_subagent_event is not None:
                        await cbs.on_subagent_event(stream_id, event, None)
            req_usage = finish_turn(handle, state)
        finally:
            server.drop_thread(handle)
        usage = RunUsage(
            requests=1,
            input_tokens=req_usage.input_tokens,
            output_tokens=req_usage.output_tokens,
        )
        if stream_id and cbs.on_subagent_usage is not None:
            await cbs.on_subagent_usage(stream_id, usage)
        return CodexRun(
            output=tx.output(), transcript=tx.messages, usage=usage, thread_id=handle.thread_id
        )

    # --- resume -----------------------------------------------------------------------
    async def resume(self, stream_id: str, meta: dict) -> tuple[str | None, str]:
        """Re-open the persisted thread and send the continuation prompt as a
        new turn, as a background job — the codex analog of
        ``CliSpawnOrchestrator.resume``."""
        thread_id = meta.get("codex_thread_id")
        if not thread_id:
            return None, (
                "The Codex thread id was never recorded for this spawn (it was "
                "interrupted before the thread started) — can't resume."
            )
        type_ = str(meta.get("type") or "")
        task = str(meta.get("task") or "")
        defn = self._resolve_agent(type_)
        if defn is None:
            return None, f"Unknown agent type {type_!r} — can't resume."
        if defn.backend != BACKEND:
            return None, f"Agent {type_!r} is no longer a codex-cli agent — can't resume."
        iso = None
        branch = meta.get("isolation")
        if branch:
            iso, err = SpawnWorktree.reopen(self.deps.workspace.root, str(branch))
            if err is not None:
                return None, err
        prior = self._transcripts.read(stream_id) or []
        label = f"{type_}: resumed — {task}"
        job_id = self.deps.jobs.register(
            "agent",
            label,
            self.execute(
                defn,
                CONTINUATION_PROMPT,
                iso.path if iso else None,
                iso,
                None,
                meta.get("max_output_chars"),
                meta.get("model"),
                stream_id,
                background=True,
                resume_thread_id=str(thread_id),
                original_task=task,
                depth=int(meta.get("depth") or 1),
                transcript_prefix=prior,
            ),
            stream_id=stream_id,
            prompt=task,
        )
        return job_id, f"Resumed as {job_id}."
