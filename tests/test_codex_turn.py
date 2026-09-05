"""`turn_events` against a hand-fed handle — no app-server process."""

from __future__ import annotations

import asyncio

import pytest

from marim_harness.codex.server import CodexServer, ThreadHandle
from marim_harness.codex.turn import TurnState, turn_events

pytestmark = pytest.mark.anyio


async def _decline(method, params):
    return {"decision": "denied"}


def _handle() -> ThreadHandle:
    return ThreadHandle(thread_id="t", events=asyncio.Queue(), request_handler=_decline)


def _completed(turn_id: str) -> tuple[str, dict]:
    return "turn/completed", {"threadId": "t", "turn": {"id": turn_id, "status": "completed"}}


async def test_completion_that_beat_start_turn_still_ends_the_turn():
    """The reader can dispatch `turn/completed` before `start_turn` resumes,
    so by the time the loop starts the handle has NO current turn. The loop
    must key on the id `start_turn` returned, not on the handle — otherwise
    the completion reads as stale and the turn hangs until the idle timeout."""
    server = CodexServer(binary="unused", timeout=2.0)
    handle = _handle()
    handle.note_turn_completed(_completed("turn-1")[1])  # arrived first
    assert handle.current_turn_id is None
    handle.events.put_nowait(("item/agentMessage/delta", {"itemId": "m", "delta": "hi"}))
    handle.events.put_nowait(_completed("turn-1"))
    state = TurnState()
    items = [item async for item in turn_events(server, handle, state, turn_id="turn-1")]
    assert state.done is not None and state.done.status == "completed"
    assert [getattr(i, "delta", None) for i in items] == ["hi"]


async def test_stale_completion_from_the_previous_turn_is_skipped():
    server = CodexServer(binary="unused", timeout=2.0)
    handle = _handle()
    handle.current_turn_id = "turn-2"
    handle.events.put_nowait(_completed("turn-1"))  # the interrupted predecessor
    handle.events.put_nowait(_completed("turn-2"))
    state = TurnState()
    _ = [item async for item in turn_events(server, handle, state, turn_id="turn-2")]
    assert state.done is not None
    assert handle.events.empty()
