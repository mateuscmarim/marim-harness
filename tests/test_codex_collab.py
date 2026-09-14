"""``codex/collab.py``: Codex collab tool calls + adopted child traffic →
first-class ``spawn_agent`` cards (pure, no server)."""

from __future__ import annotations

from typing import cast

from marim_harness.codex.collab import (
    DETACHED_RESULT,
    CollabRouter,
    LedgerOnly,
    Routed,
)
from marim_harness.codex.translate import (
    ActivityEnd,
    ActivityStart,
    AgentPing,
    CollabCall,
    CollabDone,
    Notice,
    TextDelta,
    UsageUpdate,
)


class _Hooks:
    def __init__(self) -> None:
        self.adopted: list[str] = []
        self.spawner: dict[str, str] = {}  # child id → the thread it was adopted under
        self.released: list[str] = []

    def adopt(self, child_id: str, spawner_id: str) -> None:
        self.adopted.append(child_id)
        self.spawner[child_id] = spawner_id

    def router(self, parent: str = "t1") -> CollabRouter:
        return CollabRouter(parent, adopt=self.adopt, release=self.released.append)


def _spawn(item_id: str = "k1", receivers: tuple[str, ...] = ("c1",), **kw) -> CollabCall:
    return CollabCall(
        item_id=item_id,
        tool="spawnAgent",
        receivers=receivers,
        prompt=kw.get("prompt", "review the diff"),
        model=kw.get("model", "gpt-5.4-mini"),
        effort=None,
        states=kw.get("states", {}),
    )


def _done(
    item_id: str = "k1",
    tool: str = "spawnAgent",
    receivers: tuple[str, ...] = ("c1",),
    states: dict | None = None,
    status: str = "completed",
    result: str = "",
    is_error: bool = False,
) -> CollabDone:
    return CollabDone(item_id, tool, receivers, status, states or {}, result, is_error)


def _child_msg(tid: str, item_id: str, text: str) -> tuple[str, dict]:
    return "item/agentMessage/delta", {"threadId": tid, "itemId": item_id, "delta": text}


def test_spawn_agent_becomes_a_spawn_agent_card_and_adopts_the_child():
    hooks = _Hooks()
    r = hooks.router()
    [start] = r.route_item(_spawn())
    start = cast(ActivityStart, start)
    assert start.item_id == "k1" and start.tool_name == "spawn_agent"
    assert start.args == {
        "type": "codex-agent",
        "task": "review the diff",
        "description": "",
        "model": "gpt-5.4-mini",
        "backend": "codex-cli",
        "thread_id": "c1",
    }
    assert hooks.adopted == ["c1"] and r.open_children == {"c1"}
    # The completion of the spawn call itself (agent now "running") adds nothing.
    assert r.route_item(_done(states={"c1": {"status": "running"}})) == []
    # Non-collab parent items pass through untouched.
    text = TextDelta("m1", "hi")
    assert r.route_item(text) == [text]


def test_child_traffic_is_translated_and_routed_to_the_card_stream():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    # The parent's own notifications are not the router's business.
    assert r.route(*_child_msg("t1", "m0", "parent")) is None
    assert r.route("account/updated", {}) is None
    out = r.route(*_child_msg("c1", "m1", "child says"))
    assert out == [Routed("k1", TextDelta("m1", "child says"), None, "codex-cli:gpt-5.4-mini")]
    # The model rides only the first routed item.
    out = r.route(*_child_msg("c1", "m1", " more"))
    assert out == [Routed("k1", TextDelta("m1", " more"), None, None)]
    # Usage accumulates per child and is snapshotted on the routed item.
    usage = {
        "threadId": "c1",
        "tokenUsage": {
            "total": {"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 5},
            "last": {"inputTokens": 100, "outputTokens": 20, "cachedInputTokens": 5},
        },
    }
    [routed] = r.route("thread/tokenUsage/updated", usage)
    routed = cast(Routed, routed)
    assert isinstance(routed.item, UsageUpdate)
    assert routed.usage is not None
    assert (routed.usage.requests, routed.usage.input_tokens, routed.usage.output_tokens) == (
        1,
        100,
        20,
    )
    assert routed.usage.cache_read_tokens == 5


def test_traffic_for_unmapped_threads_is_dropped_not_raised():
    hooks = _Hooks()
    r = hooks.router()
    assert r.route(*_child_msg("zz", "m1", "who?")) == []
    assert r.route("thread/started", {"thread": {"id": "zz"}}) == []


def test_agent_state_completed_settles_the_card_with_the_childs_last_message():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    r.route(*_child_msg("c1", "m1", "first"))
    r.route(*_child_msg("c1", "m2", "the answer"))
    r.route(*_child_msg("c1", "m2", " is 42"))
    # A `wait` on the child completes with the child's terminal state.
    wait = CollabCall("k2", "wait", ("c1",), None, None, None, {})
    [notice] = r.route_item(wait)
    assert notice == Routed("k1", Notice("wait"), None, None)
    out = r.route_item(
        _done("k2", "wait", states={"c1": {"status": "completed", "message": "done"}})
    )
    assert out == [ActivityEnd("k1", "the answer is 42", False)]
    assert r.open_children == frozenset()
    # Completed ≠ gone: the thread stays adopted so a later sendInput can reopen it.
    assert hooks.released == []
    # Terminal state repeated (a second wait) does not settle twice.
    assert r.route_item(_done("k3", "wait", states={"c1": {"status": "completed"}})) == []


def test_agent_state_message_and_result_are_the_content_fallbacks():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    out = r.route_item(_done(states={"c1": {"status": "errored", "message": "boom"}}))
    assert out == [ActivityEnd("k1", "boom", True)]
    r.route_item(_spawn("k2", ("c2",)))
    out = r.route_item(_done("k2", states={"c2": {"status": "completed"}}, result="ok then"))
    assert out == [ActivityEnd("k2", "ok then", False)]


def test_shutdown_and_not_found_release_the_thread():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    close = _done("k9", "closeAgent", states={"c1": {"status": "shutdown"}})
    assert r.route_item(close) == [ActivityEnd("k1", "", False)]
    assert hooks.released == ["c1"]
    # Once released, the child's traffic is unknown again.
    assert r.route(*_child_msg("c1", "m1", "late")) == []
    assert r.label_for("c1") == "agent"  # not the parent, not mapped: bare


def test_spawn_failure_without_a_thread_settles_the_card_as_an_error():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn(receivers=()))
    assert hooks.adopted == []
    out = r.route_item(_done(receivers=(), status="failed", result="no agent slots", is_error=True))
    assert out == [ActivityEnd("k1", "no agent slots", True)]


def test_receivers_known_only_at_completion_bind_then():
    hooks = _Hooks()
    r = hooks.router()
    [start] = r.route_item(_spawn(receivers=()))
    assert cast(ActivityStart, start).args["thread_id"] is None
    assert r.route_item(_done(receivers=("c1",), states={"c1": {"status": "running"}})) == []
    assert hooks.adopted == ["c1"] and r.open_children == {"c1"}


def test_follow_ups_are_notices_and_reopen_a_settled_child():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    r.route_item(_done(states={"c1": {"status": "completed"}}))
    assert r.open_children == frozenset()
    send = CollabCall("k2", "sendInput", ("c1",), "now fix it", None, None, {})
    out = r.route_item(send)
    # The settled card had its return in the ledger; putting the agent back to
    # work re-opens it (ledger-only, `resumed`) ahead of the notice.
    assert len(out) == 2 and isinstance(out[0], LedgerOnly)
    assert isinstance(out[0].item, ActivityStart) and out[0].item.args["resumed"] is True
    assert out[1] == Routed("k1", Notice("sendInput: now fix it"), None, "codex-cli:gpt-5.4-mini")
    assert r.open_children == {"c1"}
    # listAgents (and unknown tools) show nothing.
    assert r.route_item(CollabCall("k3", "listAgents", (), None, None, None, {})) == []
    # A failed follow-up is a notice too.
    out = r.route_item(_done("k2", "sendInput", status="failed", result="gone", is_error=True))
    assert out == [Routed("k1", Notice("sendInput failed: gone"), None, None)]


def test_agent_pings_are_notices_that_label_and_can_settle():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    out = r.route_item(AgentPing("a1", "c1", "/root/reviewer", "started"))
    assert out == [
        Routed("k1", Notice("agent /root/reviewer started"), None, "codex-cli:gpt-5.4-mini")
    ]
    assert r.label_for("c1") == "agent reviewer" and r.label_for("t1") is None
    r.route(*_child_msg("c1", "m1", "looks fine"))
    out = r.route_item(AgentPing("a2", "c1", "/root/reviewer", "completed"))
    assert out == [
        Routed("k1", Notice("agent /root/reviewer completed"), None, None),
        ActivityEnd("k1", "looks fine", False),
    ]
    # A ping for an unknown thread is dropped.
    assert r.route_item(AgentPing("a3", "zz", "/root/x", "started")) == []


def test_thread_started_metadata_labels_the_child_either_order():
    hooks = _Hooks()
    r = hooks.router()
    # Metadata before the spawn item mapped the thread: stashed, then applied.
    meta = {"thread": {"id": "c1", "parentThreadId": "t1", "agentNickname": "scout"}}
    assert r.route("thread/started", meta) == []
    r.route_item(_spawn())
    assert r.label_for("c1") == "agent scout"
    # After: applied directly.
    r.route_item(_spawn("k2", ("c2",)))
    meta2 = {"thread": {"id": "c2", "parentThreadId": "t1", "agentRole": "worker"}}
    assert r.route("thread/started", meta2) == []
    assert r.label_for("c2") == "agent worker"
    # A thread/started for some other parent's child is ignored.
    other = {"thread": {"id": "c3", "parentThreadId": "elsewhere"}}
    assert r.route("thread/started", other) == []


def test_child_turn_boundaries_are_not_the_agent_finishing():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    ok = {"threadId": "c1", "turn": {"id": "u1", "status": "completed"}}
    assert r.route("turn/completed", ok) == []
    assert r.open_children == {"c1"}
    failed = {"threadId": "c1", "turn": {"id": "u2", "status": "failed", "error": {"message": "x"}}}
    out = r.route("turn/completed", failed)
    assert out == [Routed("k1", Notice("agent turn failed: x"), None, "codex-cli:gpt-5.4-mini")]


def test_seal_open_closes_running_cards_ledger_only_and_resumes_them_next_turn():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    r.route(*_child_msg("c1", "m1", "working"))
    # End of the parent turn: the ledger gets a return so the persisted history
    # never carries an unmatched spawn_agent call; the live card stays open.
    assert r.seal_open() == [LedgerOnly(ActivityEnd("k1", DETACHED_RESULT, False))]
    assert r.seal_open() == []  # idempotent
    assert r.open_children == {"c1"}
    # Next turn: the child's first traffic re-opens the ledger entry (resumed)
    # ahead of the routed item, so that turn's history has a call for the
    # return that follows.
    out = r.route(*_child_msg("c1", "m2", "still working"))
    assert len(out) == 2
    reopen = cast(LedgerOnly, out[0])
    assert isinstance(reopen.item, ActivityStart)
    assert reopen.item.item_id == "k1" and reopen.item.args["resumed"] is True
    assert reopen.item.args["task"] == "review the diff"
    assert out[1] == Routed("k1", TextDelta("m2", "still working"), None, None)
    # Settling in that turn closes it for real; nothing to seal any more.
    assert r.route_item(_done("k2", "wait", states={"c1": {"status": "completed"}})) == [
        ActivityEnd("k1", "still working", False)
    ]
    assert r.seal_open() == []


def test_nested_spawn_from_a_child_renders_on_the_childs_stream():
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    nested = {
        "threadId": "c1",
        "item": {
            "type": "collabAgentToolCall",
            "id": "k2",
            "tool": "spawnAgent",
            "prompt": "dig deeper",
            "receiverThreadIds": ["g1"],
            "status": "inProgress",
        },
    }
    out = r.route("item/started", nested)
    assert len(out) == 1
    routed = cast(Routed, out[0])
    assert routed.stream_id == "k1" and routed.model == "codex-cli:gpt-5.4-mini"
    assert isinstance(routed.item, ActivityStart) and routed.item.item_id == "k2"
    assert hooks.adopted == ["c1", "g1"] and r.open_children == {"c1", "g1"}
    # Adopted under the child that spawned it, not the root: releasing the
    # child must take the grandchild with it (the server cascades by parent).
    assert hooks.spawner == {"c1": "t1", "g1": "c1"}
    # The grandchild's own traffic streams to ITS card.
    [gtext] = r.route(*_child_msg("g1", "m1", "deep"))
    assert gtext == Routed("k2", TextDelta("m1", "deep"), None, "codex-cli:default")
    # Its completion settles on the child's stream; nested cards are never
    # sealed ledger-only (the parent's ledger only knows top-level cards).
    done = {
        "threadId": "c1",
        "item": {
            **nested["item"],
            "status": "completed",
            "agentsStates": {"g1": {"status": "completed"}},
        },
    }
    assert r.route("item/completed", done) == [
        Routed("k1", ActivityEnd("k2", "deep", False), None, None)
    ]
    assert r.seal_open() == [LedgerOnly(ActivityEnd("k1", DETACHED_RESULT, False))]


def test_resumed_child_seeds_its_usage_baseline_from_the_first_update():
    """A ``resumeAgent`` reopens a thread with history: ``total − last`` at
    the first update is the baseline, like ``turn._seeded_baseline``."""
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    first = {
        "threadId": "c1",
        "tokenUsage": {
            "total": {"inputTokens": 1000, "outputTokens": 300},
            "last": {"inputTokens": 100, "outputTokens": 30},
        },
    }
    [routed] = r.route("thread/tokenUsage/updated", first)
    usage = cast(Routed, routed).usage
    assert usage is not None and (usage.input_tokens, usage.output_tokens) == (100, 30)
    second = {
        "threadId": "c1",
        "tokenUsage": {
            "total": {"inputTokens": 1250, "outputTokens": 400},
            "last": {"inputTokens": 250, "outputTokens": 100},
        },
    }
    [routed] = r.route("thread/tokenUsage/updated", second)
    usage = cast(Routed, routed).usage
    assert usage is not None and (usage.requests, usage.input_tokens) == (2, 350)


def test_close_open_settles_every_open_card_for_real_grandchild_first():
    """A spawn's thread dies at the end of its turn and takes its children
    with it: ``close_open`` settles the cards (not ledger-only), the nested
    grandchild routed to the child's stream and closed before the child."""
    from marim_harness.codex.collab import CLOSED_RESULT

    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn())
    nested = {
        "type": "collabAgentToolCall",
        "id": "k2",
        "tool": "spawnAgent",
        "receiverThreadIds": ["g1"],
        "status": "inProgress",
    }
    r.route("item/started", {"threadId": "c1", "item": nested})
    assert r.close_open() == [
        Routed("k1", ActivityEnd("k2", CLOSED_RESULT, False), None, None),
        ActivityEnd("k1", CLOSED_RESULT, False),
    ]
    assert r.open_children == frozenset() and r.close_open() == [] and r.seal_open() == []


def test_label_for_falls_back_to_the_servers_adoption_record():
    """An approval request is labelled on the reader task, which can run
    ahead of this router dequeuing the child's ``thread/started`` (or even
    its spawn item): the name then comes from what the server recorded when
    it adopted the child there; a child nobody named is a bare ``agent``."""
    announced = {"c1": "scout"}
    r = CollabRouter(
        "t1", adopt=lambda _c, _s: None, release=lambda _: None, announced=announced.get
    )
    assert r.label_for("t1") is None
    assert r.label_for("c1") == "agent scout"  # not even spawned yet, as seen from here
    assert r.label_for("c2") == "agent"
    r.route_item(_spawn())
    assert r.label_for("c1") == "agent scout"  # spawned, thread/started not yet dequeued
    meta = {"thread": {"id": "c1", "parentThreadId": "t1", "agentNickname": "scout-2"}}
    r.route("thread/started", meta)
    assert r.label_for("c1") == "agent scout-2"  # the dequeued announcement wins


def test_thread_started_model_badges_a_child_spawned_on_the_default_model():
    """``spawnAgent.model`` is optional; the child's ``thread/started``
    names the model actually running it, so the badge (and the persisted
    args) say that instead of ``default`` — in either arrival order."""
    hooks = _Hooks()
    r = hooks.router()
    # Spawn item first, then the announcement, then the first child item.
    [start] = r.route_item(_spawn(model=None))
    assert cast(ActivityStart, start).args["model"] is None
    meta = {"thread": {"id": "c1", "parentThreadId": "t1", "model": "gpt-5.4"}}
    assert r.route("thread/started", meta) == []
    [routed] = r.route(*_child_msg("c1", "m1", "hi"))
    assert cast(Routed, routed).model == "codex-cli:gpt-5.4"
    assert cast(ActivityStart, start).args["model"] == "gpt-5.4"
    # Announcement first (the server adopted it ahead of the consumer).
    meta2 = {"thread": {"id": "c2", "parentThreadId": "t1", "model": "gpt-5.4-mini"}}
    assert r.route("thread/started", meta2) == []
    r.route_item(_spawn("k2", ("c2",), model=None))
    [routed] = r.route(*_child_msg("c2", "m2", "hi"))
    assert cast(Routed, routed).model == "codex-cli:gpt-5.4-mini"
    # The request's own model is superseded by what actually runs.
    r.route_item(_spawn("k3", ("c3",), model="gpt-5.4-mini"))
    r.route("thread/started", {"thread": {"id": "c3", "parentThreadId": "t1", "model": "o5"}})
    [routed] = r.route(*_child_msg("c3", "m3", "hi"))
    assert cast(Routed, routed).model == "codex-cli:o5"


def test_nested_spawn_announced_before_its_spawn_item_is_still_noted():
    """A grandchild's ``thread/started`` names the child as its parent; when
    the reader task dispatched it ahead of the child's spawn item, the
    announcement is stashed all the same and its name/model land on the
    nested card once the item maps it."""
    hooks = _Hooks()
    r = hooks.router()
    r.route_item(_spawn(model=None))
    meta = {
        "thread": {"id": "g1", "parentThreadId": "c1", "agentNickname": "digger", "model": "o5"}
    }
    assert r.route("thread/started", meta) == []
    stranger = {"thread": {"id": "x1", "parentThreadId": "someone-else"}}
    assert r.route("thread/started", stranger) == []
    assert r.label_for("g1") == "agent digger" and r.label_for("x1") == "agent"
    nested = {
        "threadId": "c1",
        "item": {
            "type": "collabAgentToolCall",
            "id": "k2",
            "tool": "spawnAgent",
            "receiverThreadIds": ["g1"],
            "status": "inProgress",
        },
    }
    [start] = r.route("item/started", nested)
    assert cast(ActivityStart, cast(Routed, start).item).args["model"] == "o5"
    [routed] = r.route(*_child_msg("g1", "m1", "deep"))
    assert cast(Routed, routed).model == "codex-cli:o5"
    assert hooks.spawner == {"c1": "t1", "g1": "c1"}
