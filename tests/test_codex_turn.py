"""`turn_events` against a hand-fed handle — no app-server process."""

from __future__ import annotations

import asyncio

import pytest

from marim_harness.codex.server import CodexServer, ThreadHandle
from marim_harness.codex.turn import TurnState, turn_events
from marim_harness.config.context_report import ContextReport

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


def _usage(total: dict, last: dict, window: int | None = None) -> tuple[str, dict]:
    usage: dict = {"total": total, "last": last}
    if window is not None:
        usage["modelContextWindow"] = window
    return ("thread/tokenUsage/updated", {"tokenUsage": usage})


async def test_usage_updates_fold_into_the_context_report():
    """Each ``last`` is the prompt size of the newest request (cache-inclusive
    ``inputTokens``); the window is learned from the update that carries it
    and kept when a later one does not. Start-of-turn snapshots with no
    ``last`` are not reported as an empty context, and every reading is
    pushed to ``on_context`` as it lands (the status bar reads it mid-turn)."""
    server = CodexServer(binary="unused", timeout=2.0)
    handle = _handle()
    handle.events.put_nowait(_usage({"inputTokens": 100}, {}))
    handle.events.put_nowait(_usage({"inputTokens": 140}, {"inputTokens": 40}, 272_000))
    handle.events.put_nowait(_usage({"inputTokens": 190}, {"inputTokens": 50}))
    handle.events.put_nowait(_completed("turn-1"))
    seen: list[ContextReport] = []
    state = TurnState(on_context=seen.append)
    _ = [item async for item in turn_events(server, handle, state, turn_id="turn-1")]
    assert seen == [ContextReport(40, 272_000), ContextReport(50, 272_000)]
    assert state.context == ContextReport(50, 272_000)


async def test_turn_without_usage_leaves_the_context_unknown():
    server = CodexServer(binary="unused", timeout=2.0)
    handle = _handle()
    handle.events.put_nowait(_completed("turn-1"))
    state = TurnState()
    _ = [item async for item in turn_events(server, handle, state, turn_id="turn-1")]
    assert state.context is None


async def test_router_routes_child_traffic_and_turns_collab_items_into_cards():
    """With ``state.router`` set, a child's notifications (same queue — the
    thread was adopted) come out as ``Routed`` wrappers keyed by the spawn
    card, the parent's ``spawnAgent`` becomes a bare ``spawn_agent``
    ``ActivityStart``, and a child's ``turn/completed`` never ends the
    PARENT turn (and is not the parent's stale completion either)."""
    from marim_harness.codex.collab import CollabRouter, Routed
    from marim_harness.codex.translate import ActivityStart, TextDelta

    server = CodexServer(binary="unused", timeout=2.0)
    handle = _handle()
    handle.current_turn_id = "turn-1"
    spawn = {
        "type": "collabAgentToolCall",
        "id": "k1",
        "tool": "spawnAgent",
        "prompt": "look",
        "receiverThreadIds": ["c1"],
        "status": "inProgress",
    }
    child_text = {"threadId": "c1", "itemId": "m", "delta": "child"}
    child_done = {"threadId": "c1", "turn": {"id": "u9", "status": "completed"}}
    parent_text = {"threadId": "t", "itemId": "p", "delta": "parent"}
    handle.events.put_nowait(("item/started", {"threadId": "t", "item": spawn}))
    handle.events.put_nowait(("item/agentMessage/delta", child_text))
    handle.events.put_nowait(("turn/completed", child_done))
    handle.events.put_nowait(("item/agentMessage/delta", parent_text))
    handle.events.put_nowait(_completed("turn-1"))
    adopted: list[str] = []
    router = CollabRouter("t", adopt=adopted.append, release=lambda _tid: None)
    state = TurnState(router=router)
    items = [item async for item in turn_events(server, handle, state, turn_id="turn-1")]
    assert adopted == ["c1"]
    assert isinstance(items[0], ActivityStart) and items[0].tool_name == "spawn_agent"
    assert items[1] == Routed("k1", TextDelta("m", "child"), None, "codex-cli:default")
    assert items[2] == TextDelta("p", "parent")
    assert len(items) == 3 and state.done is not None and state.done.status == "completed"
