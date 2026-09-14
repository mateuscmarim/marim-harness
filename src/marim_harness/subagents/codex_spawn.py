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

import contextlib
import logging
import os
import time
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pydantic_ai.usage import RunUsage

from ..codex.approvals import ApprovalBroker, UiSeams, policy_for, sandbox_for, sandbox_mode_for
from ..codex.collab import (
    CLOSED_RESULT,
    ChildSinks,
    ChildStreams,
    CollabRouter,
    LedgerOnly,
    Routed,
    router_for,
)
from ..codex.env import CODEX_MODEL_ENV, INSTALL_HINT, CodexUnavailable, codex_available
from ..codex.server import CodexServer, ThreadHandle, ThreadOptions, TurnOptions, shared_server
from ..codex.transcript import ItemTranscript
from ..codex.turn import TurnState, finish_turn, text_input, turn_events
from ..config.codex_cli_model import effort_for
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


# What a still-open nested card reads when the spawn's turn ended abnormally
# (vs. ``CLOSED_RESULT`` for a turn that ran to completion).
ABORTED_RESULT = "still running when the spawn was aborted (thread closed)"


@dataclass
class CodexRun:
    output: str
    transcript: list[Any]
    usage: RunUsage
    thread_id: str | None
    # Codex-side sub-agents (collab ``spawnAgent``), each keyed by the stream
    # id its nested card streamed under — the codex analog of the claude-cli
    # demux's ``child_transcripts``; see ``codex/collab.py``.
    child_transcripts: dict[str, list[Any]] = field(default_factory=dict)


@dataclass
class _SpawnFeed:
    """One spawn turn's item consumer: the spawn's own items fold into its
    transcript (checkpointed as it grows, events streamed to its card); its
    Codex-side children's traffic (``Routed``) goes to their nested cards,
    and ``drain`` settles those cards however the turn ends."""

    tx: ItemTranscript
    children: ChildStreams
    router: CollabRouter
    stream_id: str | None
    on_event: Callable[[str, object, object], Awaitable[None]] | None
    checkpoint: Callable[[list, str | None], None] | None
    _ckpt_len: int = 0

    async def drain(self, items: AsyncIterator[object]) -> None:
        """Consume the turn's items. A child still running when the spawn's
        turn ends dies with the thread (dropped by the caller, cascading to
        its children), so its card is settled for real — on the normal end
        AND when the turn blows up (app-server exit, idle timeout, a sink
        raising, cancellation): the nested card must not spin forever and
        the checkpointed transcript must not end on an unanswered
        ``spawn_agent`` call. A close that fails after the turn already
        failed must not mask the turn's own error."""
        finished = False
        try:
            async for item in items:
                await self.consume(item)
            finished = True
        finally:
            result = CLOSED_RESULT if finished else ABORTED_RESULT
            with contextlib.suppress(Exception):
                for item in self.router.close_open(result):
                    await self.consume(item)

    async def consume(self, item: object) -> None:
        if isinstance(item, Routed):
            await self.children.deliver(item)
            return
        if isinstance(item, LedgerOnly):
            # A settled child put back to work (a same-turn ``sendInput``):
            # the re-opened ``spawn_agent`` call goes into the transcript —
            # the spawn's persisted record, where the main-loop model has its
            # ledger — so the settle that follows has a call to answer; the
            # live nested card already exists, so no event is streamed.
            self.tx.feed(item.item)
            self._checkpoint()
            return
        events = self.tx.feed(item)
        self._checkpoint()
        for event in events:
            if self.stream_id and self.on_event is not None:
                await self.on_event(self.stream_id, event, None)

    def _checkpoint(self) -> None:
        # Checkpoint whenever the transcript grows (mirrors
        # cli_backend._consume's growth-gated checkpoint) so a cancellation
        # mid-turn loses at most the segment since the last item, not the
        # whole run.
        if self.checkpoint is not None and len(self.tx.messages) != self._ckpt_len:
            self._ckpt_len = len(self.tx.messages)
            self.checkpoint(self.tx.messages, None)


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
                child_transcripts=result.child_transcripts,
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
        # Codex-side sub-agents: the spawn's thread owns a collab router, so a
        # ``spawnAgent`` becomes a nested ``spawn_agent`` card under this
        # spawn's card and the child's own traffic streams into that nested
        # card through the same four sinks (``Routed``). The child's approval
        # requests reach ``broker`` (an adopted thread shares its parent's
        # handler), labelled with the agent's name.
        router = router_for(server, handle)
        broker.label_for = router.label_for
        state = TurnState(router=router)
        feed = _SpawnFeed(
            ItemTranscript(),
            ChildStreams(self._child_sinks()),
            router,
            stream_id,
            cbs.on_subagent_event,
            request.checkpoint,
        )
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
            await feed.drain(turn_events(server, handle, state, turn_id=turn_id))
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
            output=feed.tx.output(),
            transcript=feed.tx.messages,
            usage=usage,
            thread_id=handle.thread_id,
            child_transcripts=feed.children.transcripts,
        )

    def _child_sinks(self) -> ChildSinks:
        cbs = self.deps.ui
        return ChildSinks(
            on_event=cbs.on_subagent_event,
            on_model=cbs.on_subagent_model,
            on_notice=cbs.on_subagent_notice,
            on_usage=cbs.on_subagent_usage,
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
