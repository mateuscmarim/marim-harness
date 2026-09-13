"""One Codex turn, driven to completion.

The event loop shared by the main-loop model (``config/codex_cli_model.py``)
and the sub-agent backend (``subagents/codex_spawn.py``): pull notifications
off the thread's queue, translate them, hand text/activity items to the
caller, and fold usage + completion into a ``TurnState``. Cancellation
interrupts the turn but keeps the thread (the next turn reuses it); a dead
server surfaces as ``CliModelError`` carrying the stderr tail; idling past the
server's timeout interrupts the turn and raises.
"""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field

from pydantic_ai.usage import RequestUsage

from ..config.context_report import ContextReport
from ..config.external_cli import CliModelError
from .server import CLOSED, CodexServer, ThreadHandle
from .translate import ItemTranslator, TurnDone, TurnFailure, UsageUpdate

_USAGE_KEYS = ("inputTokens", "outputTokens", "cachedInputTokens", "reasoningOutputTokens")


@dataclass
class TurnState:
    """What one ``turn/start`` accumulates while its notifications stream."""

    translator: ItemTranslator = field(default_factory=ItemTranslator)
    usage_total: dict = field(default_factory=dict)
    # The turn's FIRST usage update, kept whole (total + last) so finish_turn
    # can seed a resumed thread's baseline from it.
    first_usage: UsageUpdate | None = None
    done: TurnDone | None = None
    failure: str | None = None
    # The newest usage update's context reading (``last.inputTokens`` —
    # Codex's inputTokens is cache-inclusive — against the model window),
    # published through ``on_context`` as it streams so the status bar moves
    # mid-turn; None until the first update.
    context: ContextReport | None = None
    on_context: Callable[[ContextReport], None] | None = None


def text_input(text: str) -> dict:
    """A ``turn/start``/``turn/steer`` text input item."""
    return {"type": "text", "text": text, "text_elements": []}


def usage_from_total(total: dict) -> RequestUsage:
    return RequestUsage(
        input_tokens=int(total.get("inputTokens") or 0),
        output_tokens=int(total.get("outputTokens") or 0),
        cache_read_tokens=int(total.get("cachedInputTokens") or 0),
    )


def delta_since(total: dict, baseline: dict) -> dict:
    """Codex reports cumulative thread totals; marim wants per-turn usage."""
    return {k: int(total.get(k) or 0) - int(baseline.get(k) or 0) for k in _USAGE_KEYS}


def _is_stale_completion(method: str, params: dict, turn_id: str | None) -> bool:
    """True for a ``turn/completed`` notification belonging to a turn other
    than the one ``turn_events`` is currently driving.

    An interrupted turn's own completion can still be in flight (queued
    behind the interrupt ack) when the NEXT turn starts on the same thread —
    both share ``handle.events``. Left unfiltered, that stale completion
    would end the new turn instantly with the wrong (usually empty) turn's
    result; the caller drops it and keeps waiting for its own."""
    if method != "turn/completed":
        return False
    return str((params.get("turn") or {}).get("id")) != str(turn_id)


def _fold(item: object, state: TurnState) -> object | None:
    """Fold a translated item into ``state`` when it's turn/usage bookkeeping
    (usage totals, completion, failure); otherwise return it for the caller
    to yield."""
    if isinstance(item, UsageUpdate):
        state.usage_total = item.total
        if state.first_usage is None:
            state.first_usage = item
        _note_context(item, state)
        return None
    if isinstance(item, TurnDone):
        state.done = item
        return None
    if isinstance(item, TurnFailure):
        state.failure = item.message
        if not item.will_retry:
            state.done = TurnDone("failed", item.message)
        return None
    return item


def _note_context(item: UsageUpdate, state: TurnState) -> None:
    """Fold one usage update into the turn's context report. An update with
    no ``last`` (a start-of-turn snapshot) is skipped rather than reported
    as an empty context."""
    if not item.last:
        return
    used = int(item.last.get("inputTokens") or 0)
    window = item.model_context_window or (state.context.window if state.context else None)
    state.context = ContextReport(used, window)
    if state.on_context is not None:
        state.on_context(state.context)


async def turn_events(
    server: CodexServer, handle: ThreadHandle, state: TurnState, *, turn_id: str
) -> AsyncIterator[object]:
    """Yield translated items (TextDelta/ThinkingDelta/ActivityStart/ActivityEnd/
    Notice) for turn ``turn_id`` until it completes. Usage and completion are
    folded into ``state`` rather than yielded.

    ``turn_id`` is the value ``start_turn`` returned, passed explicitly rather
    than read back from ``handle.current_turn_id``: a fast turn can complete
    before ``start_turn`` even resumes, in which case the handle already has
    NO current turn — reading it here would make ``_is_stale_completion``
    drop the very completion this loop is waiting for and hang until the
    idle timeout (seen on the loaded 3.10 CI leg)."""
    try:
        while state.done is None:
            method, params = await asyncio.wait_for(handle.events.get(), server.timeout)
            if method == CLOSED:
                tail = str(params.get("stderr") or "").strip()
                raise CliModelError(f"codex app-server exited mid-turn: {tail or 'no stderr'}")
            if _is_stale_completion(method, params, turn_id):
                continue
            for item in state.translator.translate(method, params):
                kept = _fold(item, state)
                if kept is not None:
                    yield kept
    except asyncio.TimeoutError as exc:
        with contextlib.suppress(Exception):
            await server.interrupt(handle)
        raise CliModelError(f"codex turn idle for {server.timeout:.0f}s; interrupted") from exc
    except asyncio.CancelledError:
        with contextlib.suppress(Exception):
            await server.interrupt(handle)
        raise
    finally:
        handle.current_turn_id = None


def _seeded_baseline(handle: ThreadHandle, state: TurnState) -> dict:
    """The usage baseline this turn's delta is measured from.

    A thread started in this process has its baseline advanced turn by turn.
    A RESUMED thread (``thread/resume`` after ``--resume``, or a spawn
    resumed from its sidecar) starts with an empty baseline while Codex's
    ``total`` still carries every earlier turn's tokens — measured naively,
    the first turn after a resume would report the whole thread's history
    as its own usage (and bill it into the session ledger). The wire gives
    the fix for free: each usage update also carries ``last``, the newest
    response's own usage, so ``total − last`` at the first update is the
    thread's usage BEFORE this turn's first response — exactly the baseline
    a fresh thread would have had. For a fresh thread ``total == last`` on
    its first update, so the seed is all zeros and nothing changes.

    Best-effort: if Codex ever emits a start-of-turn snapshot repeating the
    previous ``last`` before the first new response, the seed over-reports
    by that one response — still bounded, unlike the whole-history error it
    replaces. Only ever applied when the baseline is empty."""
    if handle.usage_baseline or state.first_usage is None or not state.first_usage.last:
        return handle.usage_baseline
    return delta_since(state.first_usage.total, state.first_usage.last)


def finish_turn(handle: ThreadHandle, state: TurnState) -> RequestUsage:
    """Per-turn usage for a completed turn; raises ``CliModelError`` when the
    turn failed (or never reported completion). Advances the thread's usage
    baseline so the next turn's delta starts from here."""
    done = state.done
    if done is None or done.status == "failed":
        msg = (done.error if done else None) or state.failure or "codex turn failed"
        raise CliModelError(f"codex: {msg}")
    usage = usage_from_total(delta_since(state.usage_total, _seeded_baseline(handle, state)))
    if state.usage_total:
        handle.usage_baseline = dict(state.usage_total)
    return usage
