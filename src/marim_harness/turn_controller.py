"""Turn-lifecycle orchestration: the run_turn → approval loop → persist pipeline.

Extracted from Harness to isolate the most complex, highest-cyclomatic-load
subsystem (approval rounds, overflow retry, resumable flush, one-shot
consumables, steer buffering) from model/session/MCP lifecycle management.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from dataclasses import replace
from typing import TYPE_CHECKING, Any

from pydantic_ai import DeferredToolRequests, capture_run_messages
from pydantic_ai.messages import BinaryContent, ModelMessage
from pydantic_ai.usage import RunUsage

if TYPE_CHECKING:
    from pydantic_ai import RunContext

    from .deps import Deps, HarnessAgent
    from .hooks.dispatch import TurnHooks
    from .mcp import McpManager
    from .session import SessionController
    from .session.checkpoints import CheckpointManager

from .errors import dump_provider_error, is_context_overflow_error
from .permissions import Mode, resolve_approvals
from .turn_context import (
    actionable_error_note as _actionable_error_note,
)
from .turn_context import (
    render_checklist_block,
    wrap_turn_context,
)

logger = logging.getLogger(__name__)


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
        get_model: Callable[[], Any],
    ) -> None:
        self.agent = agent
        self.session = session
        self.checkpoints = checkpoints
        self.hooks = hooks
        self.mcp = mcp
        self.deps = deps
        self.get_model = get_model

        # One-shot turn state (consumed by _assemble_prompt, restored on failure).
        self._pending_error_note: str | None = None
        self._pending_hook_context: str | None = None
        self._pending_jobs_digest: str | None = None
        self._consumed_this_turn: tuple[str | None, str | None] = (None, None)

        # Live RunContext for mid-turn steering.
        self._active_run_ctx: RunContext[Deps] | None = None
        self._steer_buffer: list[tuple[str, list[tuple[bytes, str]] | None]] = []

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

    def _build_hooked_handler(self, base_handler: Any) -> Any:
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
        user_prompt: Any,
        deferred_results: Any,
        toolsets: Any,
        event_stream_handler: Any,
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
                        model=self.get_model(),
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

    async def run_turn(
        self,
        prompt: str,
        event_stream_handler: Any = None,
        attachments: list[tuple[bytes, str]] | None = None,
    ) -> str:
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
