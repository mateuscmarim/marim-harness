"""Turn-lifecycle orchestration: the run_turn → approval loop → persist pipeline.

Extracted from Harness to isolate the most complex, highest-cyclomatic-load
subsystem (approval rounds, overflow retry, resumable flush, one-shot
consumables, steer buffering) from model/session/MCP lifecycle management.
"""
from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterable, Callable, Sequence
from dataclasses import dataclass, replace
from enum import Enum, auto
from typing import TYPE_CHECKING, Any

from pydantic_ai import DeferredToolRequests, capture_run_messages
from pydantic_ai.messages import BinaryContent, ModelMessage
from pydantic_ai.usage import RunUsage

if TYPE_CHECKING:
    from pydantic_ai import RunContext
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.messages import AgentStreamEvent
    from pydantic_ai.models import Model
    from pydantic_ai.tools import DeferredToolResults
    from pydantic_ai.toolsets import AbstractToolset, FunctionToolset

    from ..hooks.dispatch import TurnHooks
    from ..mcp import McpManager
    from ..session import SessionController
    from ..session.checkpoints import CheckpointManager
    from .deps import Deps, HarnessAgent

from ..compaction import estimate_tokens, last_request_input_tokens
from ..tools.names import LSP_TOOLS
from .context import (
    actionable_error_note as _actionable_error_note,
)
from .context import (
    plan_mode_preamble,
    render_checklist_block,
    render_shell_results_block,
    wrap_turn_context,
)
from .errors import (
    CONTEXT_CONTENTION_HELP,
    CONTEXT_OVERFLOW_HELP,
    ContextWindowExceededError,
    dump_provider_error,
    is_context_overflow_error,
    overflow_is_contention,
)
from .permissions import Mode, resolve_approvals
from .toolsets import compose_turn_toolsets
from .ttft import TtftTrackingModel

logger = logging.getLogger(__name__)

# Total character budget for pending `!` passthrough results awaiting the next
# turn. run_bash caps each individual output, but a burst of `!` commands could
# still stack an unbounded prefix onto one prompt — the queue drops oldest
# entries past this, and the rendered block notes how many were elided.
_SHELL_RESULTS_BUDGET = 20_000

# How long to wait before the single in-place retry of an overflow classified
# as pool contention (see _run_with_approval). Long enough for a finishing
# sub-agent request to release its share of the server's KV pool, short enough
# that a turn blocked on it still feels responsive.
_CONTENTION_BACKOFF_SECONDS = 2.0


@dataclass
class _ConsumedContext:
    hook_context: str | None = None
    jobs_digest: str | None = None


def _has_unanswered_tool_calls(history: list[ModelMessage]) -> bool:
    """True when some ToolCallPart in ``history`` has no matching ToolReturnPart.
    Such a history ends an exchange mid-flight, and every provider rejects an
    unanswered tool_use on the next request — so persisting one makes the
    session unresumable until it's manually cleared."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    calls: set[str] = set()
    returns: set[str] = set()
    for message in history:
        for part in getattr(message, "parts", []):
            if isinstance(part, ToolCallPart):
                calls.add(part.tool_call_id)
            elif isinstance(part, ToolReturnPart):
                returns.add(part.tool_call_id)
    return bool(calls - returns)


def _tool_call_is_unusable(part) -> bool:
    """True when ``part`` is a ``ToolCallPart`` no provider will accept back in
    history because it's structurally broken: its function name never streamed (an
    empty ``tool_name``), or its arguments arrived as a string that isn't valid
    JSON. A flaky model/provider produces both — the first 400s the next request
    with "tool_calls[i] is missing a function name", the second with "Assistant
    tool call function.arguments must be valid JSON". Args given as a dict (already
    structured) or an empty/absent value (a no-arg call) are fine and kept."""
    from pydantic_ai.messages import ToolCallPart

    if not isinstance(part, ToolCallPart):
        return False
    if not part.tool_name:
        return True
    args = part.args
    if isinstance(args, str) and args.strip():
        try:
            json.loads(args)
        except (ValueError, TypeError):
            return True
    return False


def _drop_nameless_tool_calls(history: list[ModelMessage]) -> list[ModelMessage]:
    """Return a history with every structurally-unusable ``ToolCallPart`` (and the
    returns it orphans) removed — see :func:`_tool_call_is_unusable` for what
    counts: a call whose function name never arrived, or whose args string won't
    parse as JSON. Persisted, every provider then rejects the next request
    ("tool_calls[i] is missing a function name" / "function.arguments must be valid
    JSON"), wedging the session just like a dangling call does. The unanswered-call
    repair can't catch it — the part has an id and (sometimes) a name, it's just
    broken — so it needs its own pass. A ``ToolReturnPart`` that answered a dropped
    call is dropped too (it would now reference nothing), and a message left with
    no parts is removed rather than sent empty. Returns the input list unchanged
    when nothing is broken, so callers can skip a redundant persist."""
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    # Hot path: this runs as a ProcessHistory capability before EVERY model
    # request and almost always finds nothing, so do the cheapest possible check
    # first and bail before building the id set / second rebuild pass.
    #
    # Why this still has to look at the *whole* history rather than just the
    # freshly-appended tail: pydantic-ai hands a history processor a COPY of the
    # run's messages (``_agent_graph``: ``messages=ctx.state.message_history[:]``)
    # and uses our return value only for that one request — it never writes the
    # cleaned list back into ``ctx.state.message_history``. So stripping a
    # broken part is ephemeral: the malformed ModelResponse stays in the run's
    # own history and gets *buried* as later steps append after it, and every
    # subsequent request must re-strip it from somewhere in the middle. A
    # tail-only short-circuit would therefore miss a buried broken call and let
    # the provider reject the next request — the exact wedge this guards against.
    if not any(
        _tool_call_is_unusable(part)
        for message in history
        for part in getattr(message, "parts", [])
    ):
        return history
    broken_ids = {
        part.tool_call_id
        for message in history
        for part in getattr(message, "parts", [])
        if _tool_call_is_unusable(part)
    }
    cleaned: list[ModelMessage] = []
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
                and part.tool_call_id in broken_ids
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
    repaired: list[ModelMessage] = []
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


class _RunRetry(Enum):
    """How a failed ``agent.run`` round may be retried. Members double as the
    one-shot latch keys in the ``retried`` set ``_run_with_approval`` threads
    through ``_handle_run_failure`` — each kind gets exactly one shot per turn
    (a contention retry doesn't consume the compaction retry, and vice versa)."""

    CONTENTION = auto()  # pool contention: retry in place after a backoff
    COMPACTED = auto()   # genuine overflow: history force-compacted; retry


class TurnController:
    """Drives one user turn to completion through approval rounds.

    Owns the mutable turn-state that formerly lived on ``Harness``:
    pending error notes, hook context, jobs digest, steer buffer, and the
    active RunContext for mid-turn steering.
    """

    def __init__(
        self,
        agent: HarnessAgent,
        session: SessionController,
        checkpoints: CheckpointManager,
        hooks: TurnHooks,
        mcp: McpManager,
        deps: Deps,
        get_model: Callable[[], Model],
        lsp_toolset: FunctionToolset[Deps] | None = None,
    ) -> None:
        self.agent = agent
        self.session = session
        self.checkpoints = checkpoints
        self.hooks = hooks
        self.mcp = mcp
        self.deps = deps
        self.get_model = get_model
        self.lsp_toolset = lsp_toolset

        # One-shot turn state (consumed by _assemble_prompt, restored on failure).
        self._pending_error_note: str | None = None
        self._pending_hook_context: str | None = None
        self._pending_jobs_digest: str | None = None
        self._consumed_this_turn: _ConsumedContext = _ConsumedContext()

        # `!` passthrough results awaiting the next turn's prompt (see
        # add_shell_result). Not restored on turn failure: the outputs are still
        # on the user's screen and re-runnable, unlike hook context / digests.
        self._pending_shell_results: list[tuple[str, str]] = []
        self._shell_results_dropped = 0

        # Live RunContext for mid-turn steering.
        self._active_run_ctx: RunContext[Deps] | None = None
        self._steer_buffer: list[tuple[str, list[tuple[bytes, str]] | None]] = []
        # Steers flushed onto the current round's ctx but not yet confirmed
        # delivered. Flushing only *schedules* a steer (pydantic-ai drains
        # 'asap' content at the next request boundary), so a round that fails —
        # or finishes — before one would silently drop it. Each entry carries
        # the steer's (text, attachments) plus its delivery receipt: the
        # PendingMessage that enqueue appended and the run's shared queue list
        # (the queue outlives the RunContext, which is nulled before reclaim
        # runs). The round's end re-buffers whatever is still queued (see
        # _reclaim_undelivered_steers). The PendingMessage/queue slots are
        # typed `object`/`Sequence[object]` on purpose — reclaim only ever
        # identity-checks them, and `pydantic_ai._enqueue.PendingMessage` is
        # a private name.
        self._inflight_steers: list[
            tuple[str, list[tuple[bytes, str]] | None, object, Sequence[object]]
        ] = []

    def _turn_model(self) -> Model:
        """The model for this run round: the live current model, wrapped for
        client-side TTFT reporting when a UI is listening. Wrapped fresh per
        round and never stored, so the raw model object's identity — which
        /model switching and the claude-cli wiring key on — is untouched."""
        model = self.get_model()
        on_ttft = self.deps.ui.on_ttft
        if on_ttft is not None:
            model = TtftTrackingModel(model, on_ttft=on_ttft)
        return model

    def apply_session_start_context(self, ctx: str) -> None:
        """Stash SessionStart-injected context for the next turn's prompt."""
        self._pending_hook_context = ctx

    def clear_pending_jobs_digest(self) -> None:
        """Drop any re-stashed jobs digest (conversation context changed)."""
        self._pending_jobs_digest = None

    def clear_pending_shell_results(self) -> None:
        """Drop queued `!` passthrough results (conversation context changed).
        Called on /clear, /new, and session switch for the same reason the jobs
        digest is dropped there: the queue belongs to a conversation that is no
        longer active, and injecting it would tell the model the user is looking
        at output that is no longer on their screen (or belongs to another
        session entirely)."""
        self._pending_shell_results = []
        self._shell_results_dropped = 0

    def add_shell_result(self, command: str, output: str) -> None:
        """Queue a user-run `!` passthrough result for the next turn's prompt.

        Bounded: once the pending set exceeds the character budget the oldest
        entries are dropped (and counted, so the rendered block can say so) —
        a burst of `!` commands must not stack an unbounded prefix onto the
        next prompt. The newest entry is always kept even if it alone exceeds
        the budget; run_bash already caps any single output."""
        self._pending_shell_results.append((command, output))
        total = sum(len(c) + len(o) for c, o in self._pending_shell_results)
        while total > _SHELL_RESULTS_BUDGET and len(self._pending_shell_results) > 1:
            c, o = self._pending_shell_results.pop(0)
            total -= len(c) + len(o)
            self._shell_results_dropped += 1

    async def _contention_backoff(self) -> None:
        """Pause before retrying an overflow classified as pool contention —
        the shared KV cache frees up as the concurrent requests (typically
        parallel sub-agents) finish, so a short wait is the whole recovery.
        A method (not an inline sleep) so tests can stub the delay out."""
        await asyncio.sleep(_CONTENTION_BACKOFF_SECONDS)

    async def _maybe_compact(self, *, force: bool = False) -> bool:
        # When compaction actually shrinks the history, the checkpoints captured
        # against the old (absolute) indices are stale — rewinding to one would
        # slice the restructured history at the wrong boundary. Drop them so a
        # later rewind can't corrupt the conversation. (At the between-turn call
        # sites run_turn re-snapshots after this, so the current turn keeps a
        # valid rewind point; the mid-turn overflow retry instead loses this
        # turn's rewind point — a missing checkpoint beats a corrupting one.)
        # Every compaction must go through here rather than calling
        # session.maybe_compact directly, or the invalidation is skipped.
        compacted = await self.session.maybe_compact(force=force)
        if compacted:
            self.checkpoints.invalidate_after_compaction()
        return compacted

    async def _flush_resumable(
        self, captured: list[ModelMessage], resumable: list[ModelMessage]
    ) -> None:
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

    async def _assemble_prompt(self, typed: str) -> str:
        """Build the turn's prompt from what the user ``typed``, prepending any
        pending context — a finished-jobs digest, the prior turn's actionable
        error note, SessionStart-injected context, and UserPromptSubmit hook
        output — then wrapping the injected prefix in the turn-context envelope
        so a resumed session can recover just the typed text. The one-shot notes
        and the digest are consumed here."""
        prompt = typed
        # Plan mode: tell the model it is planning so it researches deliberately
        # and ends by calling present_plan, rather than flailing into denials.
        # Prepended first so it sits just above the user's typed request.
        if self.deps.workspace.mode is Mode.plan:
            prompt = f"{plan_mode_preamble()}\n\n{prompt}"
        # Commands the user ran via the `!` passthrough since the last turn.
        # Their outputs are already on the user's screen; this drain makes them
        # model-visible. Consumed here (not restored on failure — the user can
        # re-run a ! command, unlike hook context).
        shell_block = render_shell_results_block(
            self._pending_shell_results, self._shell_results_dropped
        )
        if shell_block:
            prompt = f"{shell_block}\n\n{prompt}"
            self._pending_shell_results = []
            self._shell_results_dropped = 0
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
        self._consumed_this_turn = _ConsumedContext(
            hook_context=hook_context, jobs_digest=digest or None
        )
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
            # length. Guard the invariant with a real raise, not an `assert`:
            # under `python -O` assertions are stripped, so a future prepend that
            # broke the suffix would silently slice the envelope at the wrong
            # offset and persist a corrupted, unrecoverable turn — fail loudly.
            if not prompt.endswith(typed):
                raise RuntimeError("turn-context injection must keep `typed` as a suffix")
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
        ctx = self._active_run_ctx
        if ctx is None or not self._steer_buffer:
            return
        for text, atts in self._steer_buffer:
            # Snapshot the queue length so the receipt below can tell whether
            # enqueue actually appended: an all-empty enqueue is a documented
            # no-op, and blindly taking queue[-1] then would alias this steer
            # to the PREVIOUS one's PendingMessage, falsely reclaiming it.
            queue = ctx.pending_messages
            before = len(queue) if queue is not None else 0
            ctx.enqueue(
                text,
                *(BinaryContent(data=d, media_type=m) for d, m in (atts or [])),
                priority="asap",
            )
            # Re-read: enqueue raises if the ctx has no queue, so reaching here
            # with content means the queue exists and (for non-empty content)
            # just grew by one — its tail is this steer's delivery receipt.
            queue = ctx.pending_messages
            if queue is not None and len(queue) > before:
                self._inflight_steers.append((text, atts, queue[-1], queue))
        self._steer_buffer = []

    def _reclaim_undelivered_steers(self) -> None:
        """Re-buffer any flushed steer that never reached the model.

        Called at each round's end (success or failure). A flushed steer is
        only *scheduled*: pydantic-ai's drain capability removes its
        PendingMessage from the run's shared queue (mutated in place) when it
        folds it into a model request, and a run that ends normally redirects
        leftover 'asap' messages into one more request — so a steer is
        stranded only when the round dies (error, cancel) before its next
        request boundary, or lands in the sliver between the run's final drain
        and run() returning. Its delivery receipt is object identity: the
        PendingMessage captured at enqueue time still sitting in that queue
        means undelivered. No text matching, so a steer that echoes earlier
        history or duplicates another steer can't be conflated — the pre-2.x
        heuristic had to reconcile steer texts against the round's new
        messages (baseline slicing + a multiset) to approximate exactly this.
        Reclaimed steers go to the front of the buffer so the next flush
        (continuation round) or the TUI's take_buffered_steers (turn end) sees
        them in their original order."""
        if not self._inflight_steers:
            return
        undelivered = [
            (text, atts)
            for text, atts, pending, queue in self._inflight_steers
            if any(entry is pending for entry in queue)
        ]
        self._inflight_steers = []
        self._steer_buffer = undelivered + self._steer_buffer

    def take_buffered_steers(
        self,
    ) -> list[tuple[str, list[tuple[bytes, str]] | None]]:
        """Return and clear any steers that were never flushed (the
        finishing-gap race). The caller decides what to do with them."""
        buffered, self._steer_buffer = self._steer_buffer, []
        return buffered

    def _build_hooked_handler(
        self, base_handler: EventStreamHandler[Deps] | None
    ) -> EventStreamHandler[Deps] | None:
        """Wrap the event-stream handler to (1) capture the live RunContext for
        steering and (2) fire Pre/PostToolUse hooks on tool events. Returns
        ``None`` when there's neither a base handler nor hooks, so headless runs
        don't stream just to capture a ctx nobody steers."""
        if base_handler is None and self.deps.hooks is None:
            return None
        _call_inputs: dict[str, Any] = {}

        async def _wrapped(
            stream_ctx: RunContext[Deps],
            events: AsyncIterable[AgentStreamEvent],
        ) -> None:
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

    async def _handle_run_failure(
        self,
        exc: BaseException,
        captured: list[ModelMessage],
        resumable: list[ModelMessage],
        deferred_results: DeferredToolResults | None,
        round_usage: RunUsage,
        retried: set[_RunRetry],
    ) -> _RunRetry:
        """Resolve one failed ``agent.run`` round: bank its spend, reclaim
        undelivered steers, then either hand back a one-shot retry directive
        (contention → backoff already slept; overflow → history already
        force-compacted and persisted) or flush a resumable history, stash the
        error note, spill the provider payload, and re-raise. ``retried`` is the
        cross-round latch set: a directive is only returned if its kind isn't
        already in it, and the returned kind is added here so the caller can't
        forget to latch it."""
        # Bank whatever the failed run already spent. The provider
        # billed those tokens regardless of the abort, so dropping
        # them would make the session's running total undercount. A
        # pure in-memory add — safe even on the cancel teardown path
        # (it can't block the re-raise / Ctrl-C). Counts the failed
        # attempt on the overflow-retry path too: those tokens were
        # spent before the compaction-and-retry below.
        self.session.usage += round_usage
        # A steer flushed into this round may never have reached a
        # request boundary; put it back in the buffer (for the
        # overflow retry below, or the TUI's turn-end pickup) before
        # deciding how this failure resolves.
        self._reclaim_undelivered_steers()
        # Context-overflow recovery: the request exceeded the real
        # window despite our estimate. Force a compaction and retry the
        # run once. Only when the compaction actually shrank the history
        # (else a retry would just fail identically). The compacted
        # history is persisted by maybe_compact, so it also becomes the
        # rollback baseline for the retry. Two guards beyond the retry
        # flag: (1) never on a continuation round (deferred_results
        # set) — the in-memory history then deliberately ends with the
        # round's unanswered tool calls, and compacting would persist
        # exactly the dirty state the approval loop promises never
        # touches disk; the normal failure path below repairs and
        # persists a resumable history instead. (2) go through
        # _maybe_compact, not session.maybe_compact, so the stale
        # checkpoints are invalidated — their absolute indices point
        # into the pre-compaction history and a later /rewind through
        # one would slice at a wrong boundary.
        # A provider "context exceeded" can also mean POOL CONTENTION,
        # not an oversized request: local servers (LM Studio/llama.cpp
        # with a unified KV cache) share one window across n_parallel
        # slots, and concurrent requests — parallel sub-agents — can
        # exhaust the pool and fail EVERY in-flight request, however
        # small. Compacting can't help (the request already fits) and
        # the "window too small" diagnostic would mislead. When the
        # failing request's best-known size (the provider-measured
        # last count or the chars/4 estimate, whichever is larger) is
        # far below the KNOWN served window, classify as contention
        # and retry once in place after a short backoff — sub-agents
        # finishing is what frees the pool. Unknown window ⇒ never
        # contention, so the classic first-turn overflow keeps the
        # old behavior. Safe on continuation rounds too: unlike the
        # compaction below, the retry doesn't touch the history.
        overflow = is_context_overflow_error(exc)
        contention = overflow and overflow_is_contention(
            max(
                self.session.last_input_tokens or 0,
                estimate_tokens(list(captured) or list(self.session.history)),
            ),
            self.session.known_window,
        )
        if contention and _RunRetry.CONTENTION not in retried:
            retried.add(_RunRetry.CONTENTION)
            await self._contention_backoff()
            return _RunRetry.CONTENTION
        if (
            _RunRetry.COMPACTED not in retried
            and not contention
            and deferred_results is None
            and overflow
            and await self._maybe_compact(force=True)
        ):
            retried.add(_RunRetry.COMPACTED)
            return _RunRetry.COMPACTED
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
                    dump_provider_error, self.deps.workspace.root, exc
                ),
                timeout=0.25,
            )
        except Exception:
            logger.debug("failed to dump provider error", exc_info=True)
        # Reaching here with an overflow error means recovery failed
        # (nothing droppable — the classic first-turn case — or the
        # one retry above already overflowed again, or it was a dirty
        # continuation round). Swap the terse provider text for an
        # actionable diagnostic matching the classification; the
        # original is chained and already spilled to disk above.
        if overflow:
            raise ContextWindowExceededError(
                CONTEXT_CONTENTION_HELP if contention else CONTEXT_OVERFLOW_HELP
            ) from exc
        raise exc

    async def _resolve_approval_round(
        self, requests: DeferredToolRequests, resumable: list[ModelMessage]
    ) -> DeferredToolResults:
        """One approval round: notify (ask mode), resolve against the current
        Mode, and on any failure roll back to the last cleanly persisted
        baseline before re-raising — the in-memory history at this point ends
        with unanswered tool calls and must never be what a resumed session
        sees. See _run_with_approval for why the dirty history is never
        persisted during the round."""
        if self.deps.workspace.mode is Mode.ask and requests.approvals:
            names = ", ".join(
                getattr(c, "tool_name", None) or "(unknown)"
                for c in requests.approvals
            )
            # Belt-and-suspenders: the hook engine is already best-effort
            # (runner.dispatch never raises), but a payload-assembly bug or
            # a future non-observe-only hook must never abort the turn and
            # lose the model's in-flight work. Degrade to a logged warning.
            try:
                await self.hooks.notification(
                    "approval_needed", "Approval needed", names
                )
            except Exception:  # noqa: BLE001 — a notification must never crash a turn
                logger.warning("approval-needed notification hook failed", exc_info=True)
        try:
            deferred_results = await resolve_approvals(
                requests, self.deps.workspace.mode, self.deps.ui.request_approval
            )
        except BaseException:
            self.session.history = resumable
            # Offload the rollback write off the event loop (like the
            # failure path above). Safe even when the exception in flight
            # is a cancel: `resumable` is the last *cleanly persisted*
            # baseline, so if a second cancel interrupts this thread the
            # on-disk file is already this exact state — nothing is lost.
            # The bare `raise` re-raises the still-active exception across
            # the await, so the original error/cancel propagates intact.
            # The write itself is best-effort: a disk error here must not
            # replace the in-flight exception (an OSError surfacing
            # instead of a Ctrl-C cancel would make shutdown look like a
            # crash). Only Exception is swallowed — a cancellation of the
            # rollback write still propagates.
            try:
                await asyncio.to_thread(self.session.persist)
            except Exception:
                logger.warning(
                    "approval rollback persist failed", exc_info=True
                )
            raise
        return deferred_results

    async def _finish_turn(self, output: str) -> str:
        """The post-persist turn tail: Stop hook (must never crash a completed
        turn) and the non-blocking autoname schedule. Returns the final text."""
        # The turn has already succeeded and persisted; a failing Stop hook
        # must not turn that into a turn-level exception. Degrade like the
        # approval notification above.
        try:
            await self.hooks.stop()
        except Exception:  # noqa: BLE001 — a Stop hook must never crash a completed turn
            logger.warning("stop hook failed", exc_info=True)
        # Autoname is *scheduled*, not awaited: it's a full titler LLM
        # round-trip producing cosmetic metadata, so the turn (and the TUI's
        # busy state / queued-prompt drain behind it) must not block on it.
        # Headless settles the task before teardown via wait_autoname.
        self.session.schedule_autoname()
        return output

    async def _run_with_approval(
        self,
        user_prompt: str | list[str | BinaryContent] | None,
        deferred_results: DeferredToolResults | None,
        toolsets: Sequence[AbstractToolset[Any]] | None,
        event_stream_handler: EventStreamHandler[Deps] | None,
        resumable: list[ModelMessage],
    ) -> str:
        """Drive the agent.run loop, handling DeferredToolRequests approval rounds,
        persisting on success, and rolling back to ``resumable`` on interrupt.
        Returns the final text output."""
        # One-shot retry latches, one per _RunRetry kind — see _RunRetry and
        # _handle_run_failure for why each recovery path gets exactly one shot
        # and why they don't consume each other.
        retried: set[_RunRetry] = set()
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
                        model=self._turn_model(),
                        message_history=self.session.history,
                        deps=self.deps,
                        deferred_tool_results=deferred_results,
                        event_stream_handler=event_stream_handler,
                        toolsets=toolsets,
                        usage=round_usage,
                    )
                except BaseException as exc:
                    retry = await self._handle_run_failure(
                        exc, captured, resumable, deferred_results, round_usage, retried
                    )
                    if retry is _RunRetry.COMPACTED:
                        # The compacted-and-persisted history is the new
                        # rollback baseline for the retry (maybe_compact
                        # persisted it).
                        resumable = list(self.session.history)
                    continue
            # This round's streaming ends the moment run() returns, so the
            # captured ctx is now stale. Null it before the approval modal /
            # next-round gap so a steer arriving in that window buffers and is
            # delivered to the next round's fresh ctx, rather than being enqueued
            # onto a completed RunContext.
            self._active_run_ctx = None
            # A successful run redirects queued steers into one more request, so
            # this is normally a no-op — but a steer enqueued in the sliver
            # between the run's final drain and run() returning is still sitting
            # in the queue; re-buffer it so the continuation round (or the TUI's
            # turn-end pickup) delivers it instead of dropping it.
            self._reclaim_undelivered_steers()
            # The run reached the model and returned, so this turn's one-shot
            # consumables (hook context / jobs digest) were genuinely delivered.
            # Clear the restore-on-failure stash so a later approval-round failure
            # doesn't re-emit context the model already saw. Idempotent across
            # rounds — only the first success matters.
            self._consumed_this_turn = _ConsumedContext()
            self.session.history = result.all_messages()
            self.session.usage += result.usage
            # Record the last request's real input-token count so the next
            # compaction check gates on the provider's measurement rather than the
            # chars/4 estimate alone (which undershoots dense code ~25% and can sail
            # the session past the real window). Updated every round on the current
            # history; reset to None on clear/switch so a resumed session falls back
            # to the estimate until its first request reports usage again.
            self.session.last_input_tokens = last_request_input_tokens(self.session.history)
            if isinstance(result.output, DeferredToolRequests):
                # This history ends with unanswered tool calls; keep it in memory
                # for the continuation run but do NOT persist it. A cancel or
                # failure during approval would otherwise leave the session
                # ending in a dangling tool_use — unresumable. Roll back to the
                # last clean state if the approval round is interrupted.
                deferred_results = await self._resolve_approval_round(
                    result.output, resumable
                )
                user_prompt = None  # continuation is driven by deferred_results
                continue
            # Offload the success-path write so a multi-MB serialize+fsync doesn't
            # stall the event loop (the TUI render/input). The loop is NOT frozen
            # during the await — other tasks run and the worker thread reads
            # self.history concurrently. That's safe because nothing mutates the
            # main session history mid-turn: agent.run() has returned, subagents/
            # jobs operate on isolated histories, and rewind/reset are between-turn
            # user actions. The worker therefore serializes a stable snapshot.
            await asyncio.to_thread(self.session.persist)
            # This round completed cleanly and is persisted — it becomes the new
            # rollback baseline for any subsequent round.
            resumable = list(self.session.history)
            # Compact after the turn completes so the gauge never shows >100%
            # for long: the mid-turn growth is folded in immediately rather
            # than waiting for the next turn's start-of-turn check.
            await self._maybe_compact()
            return await self._finish_turn(result.output)

    async def run_turn(
        self,
        prompt: str,
        event_stream_handler: EventStreamHandler[Deps] | None = None,
        attachments: list[tuple[bytes, str]] | None = None,
    ) -> str:
        """Run the agent until it produces a final text answer, looping through
        any approval rounds. Returns the final text output."""
        await self._maybe_compact()
        # Self-heal a session left mid-exchange by an earlier aborted turn or a
        # flaky model, BEFORE snapshotting this turn's rewind point. Two distinct
        # malformations both make every provider reject the next request and wedge
        # the session: a nameless ToolCallPart (a partial tool call whose function
        # name never streamed) and a ToolCallPart with no matching return
        # ("unprocessed tool calls"). Strip the former, then repair the latter, so
        # the session resumes instead of raising on the next request.
        #
        # Ordering is load-bearing: the repair INSERTS a synthesized return and the
        # strip can REMOVE messages, so both change len(history). A Checkpoint
        # records an *absolute* history_len, so it must be taken AFTER this sanitize
        # — otherwise it captures the pre-sanitize length and a later /rewind slices
        # history[:stale_len] at a wrong boundary, stranding a tool call from its
        # return: exactly the unresumable, mid-pair history this whole subsystem
        # exists to prevent. Sanitizing here rather than inside the try below is
        # safe: it drains no one-shot consumables and precedes the checkpoint, so a
        # failure here leaks neither the hook context / jobs digest nor a dead
        # checkpoint (there is none yet).
        sanitized = _drop_nameless_tool_calls(self.session.history)
        repaired = _repair_unanswered_tool_calls(sanitized)
        if repaired is not self.session.history:
            self.session.history = repaired
            # Offload the sanitizing write off the event loop, consistent with the
            # success/rollback sites below.
            await asyncio.to_thread(self.session.persist)
        # Capture a rewind point for this turn before any work runs. Remember where
        # the (now-sanitized) history stands and which checkpoint this is, so a turn
        # that fails without producing any model output can roll its (dead)
        # checkpoint back. pre_turn_len is the *post-sanitize* length, so both the
        # checkpoint's history_len and _turn_produced_response measure new model
        # output against the identical boundary.
        pre_turn_len = len(self.session.history)
        # snapshot() shells out to git (``git add -A`` over the whole working tree,
        # then write-tree/commit-tree) — synchronous subprocess work whose cost
        # scales with the tree size. run_turn is an async worker on the TUI's event
        # loop, so running it inline froze the UI at the start of *every* turn. Offload
        # it like persist() below. No deadline: this is the turn's rewind point, not a
        # best-effort flush, so it must complete — and a thread can't block the loop
        # however long git takes.
        checkpoint_index = await asyncio.to_thread(self.checkpoints.snapshot, prompt)
        # Everything from prompt assembly onward runs under the try: assembly
        # drains the one-shot consumables and compose_turn_toolsets can raise on
        # a flaky MCP server, so a failure anywhere past this point must hit the same
        # restore-and-discard path as a failed run — outside the try it would
        # permanently eat the hook context / jobs digest and leak the dead
        # checkpoint snapshotted above.
        try:
            user_prompt: str | list[str | BinaryContent] | None = (
                await self._assemble_prompt(prompt)
            )
            if attachments and user_prompt is not None:
                user_prompt = [user_prompt, *(BinaryContent(data=d, media_type=m)
                                              for d, m in attachments)]
            # Tool-search policy: compose_turn_toolsets folds the LSP toolset
            # (when enabled) and the MCP/plugin surface under one deferral decision.
            # OTHER builtins (on the Agent) remain unaffected.
            toolsets = await compose_turn_toolsets(
                self.mcp,
                self.lsp_toolset,
                len(LSP_TOOLS),
                self.deps.workspace.tool_search,
                self.deps.workspace.tool_search_threshold,
            )
            # When hooks are configured, intercept each streamed tool event to fire
            # Pre/PostToolUse, then forward to the original handler (or drain if none).
            event_stream_handler = self._build_hooked_handler(event_stream_handler)
            # The last persisted, resumable history — guaranteed free of unanswered
            # tool calls. Captured once here and refreshed only after a clean
            # persist; the deferred-approval round below deliberately holds a dirty
            # history in self.session.history, so this must NOT be recomputed from it
            # per iteration (that poisoned the rollback baseline across rounds).
            resumable = list(self.session.history)
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
            consumed = self._consumed_this_turn
            self._consumed_this_turn = _ConsumedContext()
            if consumed.hook_context and not self._pending_hook_context:
                self._pending_hook_context = consumed.hook_context
            if consumed.jobs_digest and not self._pending_jobs_digest:
                self._pending_jobs_digest = consumed.jobs_digest
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
