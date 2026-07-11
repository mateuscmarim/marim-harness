"""Drives a built sub-agent's model loop to completion: transient-error retry
with resume, context-overflow shed, pool-contention classification, and the
foreground UI notices. Extracted from SubagentRunner so the runner stays the
spawn-lifecycle coordinator.
"""

from __future__ import annotations

import contextlib
import logging
from collections.abc import Callable
from typing import TYPE_CHECKING, Any

from pydantic_ai.usage import RunUsage, UsageLimits

if TYPE_CHECKING:
    from pydantic_ai.agent import EventStreamHandler
    from pydantic_ai.run import AgentRunResult

    from ..session.ctrl import SessionController

from ..compaction import estimate_tokens, last_request_input_tokens, mask_stale_observations
from ..runtime.deps import Deps, SubAgent
from ..runtime.errors import (
    is_context_overflow_error,
    is_transient_model_error,
    overflow_is_contention,
)
from .policies import RetryPolicy

logger = logging.getLogger(__name__)


@contextlib.contextmanager
def _fresh_capture():
    """A message-capture context that is ALWAYS fresh — unlike pydantic-ai's
    public ``capture_run_messages``, which this deliberately bypasses.

    Why the private API: ``capture_run_messages`` REUSES an existing contextvar
    state instead of nesting, and an ``agent.run`` binds to the captured list
    only while the state's ``used`` flag is still False. A foreground spawn runs
    INSIDE the main turn's tool execution, where ``TurnController._run_agent_loop``
    already holds a capture context that the main run has bound (flag set).
    Entering the public context there yields the MAIN turn's message list — and
    the sub-agent's run, finding ``used=True``, records its messages into a list
    nobody holds. A retry in ``run_to_completion`` would then "resume" the
    sub-agent with the orchestrator's conversation instead of its own. This
    helper reaches for pydantic-ai's private ``_messages_ctx_var`` and
    unconditionally installs a fresh ``_RunMessages`` holder, restoring the
    outer state in ``finally`` so the main turn's capture is untouched.

    The private-name coupling is a considered trade: pydantic-ai offers no
    nested/fresh mode for ``capture_run_messages`` (its docs promise only "the
    first run within the context"), and the alternative — not capturing at all —
    would forfeit sub-agent resumability. ``tests/test_subagent_retry.py`` pins
    both the private names and the outer-capture topology, so a dependency bump
    that changes either fails loudly there instead of silently corrupting
    sub-agent resumes with the main conversation.
    """
    from pydantic_ai import _agent_graph

    messages: list = []
    token = _agent_graph._messages_ctx_var.set(_agent_graph._RunMessages(messages))
    try:
        yield messages
    finally:
        _agent_graph._messages_ctx_var.reset(token)


def _resumable_history(messages: list) -> list | None:
    """Turn the conversation captured from a failed sub-agent attempt into a
    history safe to resume from, or ``None`` when there's nothing to carry (the
    request failed before any message was recorded — resume by re-sending the
    task). Reuses the main turn's two repairs so a sub-agent resume obeys the same
    provider invariant: drop a half-streamed nameless tool call, then synthesize a
    return for any tool call left unanswered when the attempt died. Imported lazily
    because ``agent`` imports this module — a top-level import would cycle."""
    if not messages:
        return None
    from ..runtime.harness import _drop_nameless_tool_calls, _repair_unanswered_tool_calls

    repaired = _repair_unanswered_tool_calls(_drop_nameless_tool_calls(messages))
    return repaired or None


class SpawnRunDriver:
    """Drives a built sub-agent's model loop to completion. Retry/overflow/
    contention recovery lives here, keeping ``SubagentRunner`` the
    spawn-lifecycle coordinator."""

    def __init__(self, deps: Deps, session: SessionController,
                 retry: RetryPolicy, known_window: Callable[[], int | None]) -> None:
        self.deps = deps
        self.session = session
        self._retry = retry
        # Reads the current session model's known window; a callable rather
        # than a value because it must reflect a runtime `/model` switch.
        self._known_window = known_window

    async def backoff(self, attempt: int) -> None:
        """Sleep before the ``attempt``-th retry via the RetryPolicy's backoff.
        Kept as a thin ``SpawnRunDriver.backoff`` method (not an inline
        ``self._retry.backoff`` call) so a test can stub it to skip the real
        sleep — see test_subagent_retry."""
        await self._retry.backoff(attempt)

    async def run_to_completion(self, sub: SubAgent, task: str, run_deps: Deps,
                                 granted: list[Any], handler: EventStreamHandler[Deps] | None,
                                 stream_id: str | None = None,
                                 history: list | None = None) -> AgentRunResult[str]:
        """Run a built sub-agent to its final result, retrying *transient* model
        errors (gateway/server hiccups, timeouts, rate limits) with backoff. A
        permanent error, or exhausting the retry budget, re-raises for the caller's
        contain/propagate path.

        A retry *resumes* the run rather than restarting it: the conversation the
        failed attempt produced (captured even though it raised) is carried forward
        as ``message_history``, so a transient blip on step 20 of a multi-step spawn
        doesn't throw away — and re-pay for — the first 19 steps. The captured
        history is sanitized and repaired the same way the main turn does before a
        resumed request (drop a half-streamed nameless tool call, synthesize a
        return for any unanswered call), or every provider rejects it. A mutating
        isolated spawn keeps whatever files the failed attempt already wrote, which
        is fine — its worktree is a throwaway branch.

        A foreground spawn (``stream_id`` set) gets an out-of-band UI notice on each
        retry so the user sees the card recover rather than silently stall.

        A context-overflow rejection (a permanent 4xx the transient path would
        surface) gets one recovery attempt of its own: the captured conversation
        is resumed with stale tool observations masked (see ``_shed_context``);
        a repeat overflow, or one with nothing left to shed, surfaces normally.

        ``history``, when given, is a persisted transcript to resume from (an
        interrupted spawn continuing after a restart) — the first attempt sends
        both ``task`` (the continuation prompt) as the run's input AND
        ``message_history=history``, so pydantic-ai appends the prompt on top of
        the prior conversation. A later transient-retry resume within the same
        call takes over from ``resume_history`` instead, exactly as before."""
        attempt = 0
        overflow_shed = False
        resume_history: list | None = None
        # One usage accumulator across ALL attempts, mirroring the controller's
        # per-round banking (see _run_with_approval): pydantic-ai mutates it in
        # place as each model step completes, so an attempt that dies mid-run
        # still leaves its spend here. On success the returned ``result.usage``
        # IS this object (agent.run threads ``usage or RunUsage()`` straight
        # into run state), so the callers' ``session.usage += result.usage``
        # already covers the failed attempts; the re-raise path below banks it
        # explicitly since no result reaches the caller there.
        run_usage = RunUsage()
        while True:
            captured: list = []
            try:
                # NOT the public capture_run_messages: a foreground spawn runs
                # inside the main turn's capture context, which the public API
                # would silently reuse — see _fresh_capture's docstring.
                with _fresh_capture() as captured:
                    return await sub.run(
                        task if resume_history is None else None,
                        message_history=(resume_history if resume_history is not None
                                         else history),
                        deps=run_deps, toolsets=granted,
                        event_stream_handler=handler,
                        usage=run_usage,
                        usage_limits=UsageLimits(request_limit=self._retry.request_limit),
                    )
            except Exception as exc:  # noqa: BLE001
                # An overflow whose request is far below the KNOWN served window
                # is pool CONTENTION, not an oversized conversation: local
                # servers (LM Studio/llama.cpp unified KV cache) share one
                # window across n_parallel slots, and a parallel spawn fan-out
                # can exhaust the pool and fail every in-flight request at
                # once. Masking observations can't free pool space held by the
                # OTHER requests — so skip the shed and ride the transient
                # retry path below instead: back off, let siblings finish, and
                # resume the captured conversation intact. An empty capture (a
                # first-request failure) sizes to 0 ⇒ never contention, and an
                # unknown window keeps the old behavior everywhere.
                overflow = is_context_overflow_error(exc)
                contention = overflow and overflow_is_contention(
                    max(
                        last_request_input_tokens(list(captured)) or 0,
                        estimate_tokens(list(captured)),
                    ),
                    self._known_window(),
                )
                # Context overflow is a permanent 4xx, so the transient path below
                # would re-raise it — but unlike a genuine bad request it IS
                # recoverable: shed the bulky old observations from the captured
                # conversation and resume once. Unlike the proactive masker (which
                # rewrites only the outgoing request), the shed is folded into the
                # resume history itself, so the freed tokens stay freed. One shot
                # only: a second overflow means masking already gave all it had.
                if not overflow_shed and overflow and not contention:
                    shed = self._shed_context(list(captured))
                    if shed is not None:
                        overflow_shed = True
                        resume_history = shed
                        logger.info(
                            "sub-agent overflowed its context; masked stale "
                            "observations and resuming"
                        )
                        await self._notice_overflow(stream_id)
                        continue
                if attempt >= self._retry.attempts or not (
                    contention or is_transient_model_error(exc)
                ):
                    # Surfacing the failure loses the result object but must not
                    # lose the spend: the provider billed the failed attempts'
                    # tokens regardless, so bank the accumulator before the
                    # re-raise. (The success path needs no counterpart — the
                    # callers fold result.usage, which IS this accumulator.)
                    self.session.usage += run_usage
                    raise
                attempt += 1
                resume_history = _resumable_history(list(captured))
                logger.info(
                    "sub-agent hit a transient error (%s); resuming, retry %d/%d "
                    "after backoff", exc.__class__.__name__, attempt,
                    self._retry.attempts,
                )
                await self._notice_retry(stream_id, exc, attempt)
                await self.backoff(attempt)

    async def _notice_retry(self, stream_id: str | None, exc: Exception,
                            attempt: int) -> None:
        """Surface a transient-error retry on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.ui.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(
            stream_id,
            f"transient error ({exc.__class__.__name__}) — "
            f"retrying {attempt}/{self._retry.attempts}…",
        )

    # Shed settings for the overflow backstop: spare only the newest observation
    # (the model may still be acting on it) and mask anything else remotely bulky.
    # Deliberately more aggressive than the proactive masker — by the time we're
    # here the provider has already rejected the request for size.
    _SHED_KEEP_RECENT = 1
    _SHED_MIN_CHARS = 64

    def _shed_context(self, messages: list) -> list | None:
        """The overflow-recovery lever: repair the captured conversation the same
        way a transient resume does, then aggressively mask stale observations.
        Returns the shrunk history to resume from, or None when masking freed
        nothing — the overflow is then unrecoverable here and must surface."""
        repaired = _resumable_history(messages)
        if not repaired:
            return None
        # Known imprecision, accepted: mask_stale_observations counts "recent"
        # newest-first across parts, so a parallel tool round wider than
        # keep_recent(=1) can mask sibling returns the model hasn't acted on
        # yet — and after the repair above, the spared "newest" return can be a
        # repair-synthesized stub rather than real output. Acceptable here: the
        # placeholder text invites the model to re-run the tool, and by this
        # point the provider has already rejected the request outright, so a
        # lossy-but-live resume beats a dead spawn.
        masked, count = mask_stale_observations(
            repaired, self._SHED_KEEP_RECENT, min_chars=self._SHED_MIN_CHARS
        )
        return masked if count else None

    async def _notice_overflow(self, stream_id: str | None) -> None:
        """Surface an overflow recovery on a foreground spawn's card. A no-op for
        a background spawn (no card) or when no UI is listening."""
        cb = self.deps.ui.on_subagent_notice
        if cb is None or not stream_id:
            return
        await cb(stream_id, "context overflow — masked stale tool output, resuming…")
