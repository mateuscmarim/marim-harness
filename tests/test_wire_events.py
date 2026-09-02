from marim_harness.server.wire_events import (
    AskPending,
    TextDelta,
    ToolCall,
    TurnFinished,
    parse_wire_event,
)


def test_parse_every_known_type():
    cases = [
        (
            {"type": "turn.started", "turn_id": "t1", "prompt": "hi", "trigger": "user"},
            "turn.started",
        ),
        (
            {"type": "turn.finished", "turn_id": "t1", "output": "ok", "usage": {}},
            "turn.finished",
        ),
        (
            {"type": "turn.finished", "turn_id": "t1", "interrupted": True},
            "turn.finished",
        ),
        ({"type": "turn.error", "turn_id": "t1", "error": "boom"}, "turn.error"),
        ({"type": "text.delta", "text": "hel"}, "text.delta"),
        ({"type": "thinking.delta", "text": "hmm"}, "thinking.delta"),
        (
            {"type": "tool.call", "id": "c1", "name": "bash", "args": {"command": "ls"}},
            "tool.call",
        ),
        (
            {"type": "tool.result", "id": "c1", "content": "ok"},
            "tool.result",
        ),
        (
            {
                "type": "ask.pending",
                "id": "a1",
                "kind": "approval",
                "payload": {},
                "created": "now",
            },
            "ask.pending",
        ),
        (
            {"type": "ask.resolved", "id": "a1", "answer": {"approve": True}},
            "ask.resolved",
        ),
        (
            {"type": "session.status", "status": "running"},
            "session.status",
        ),
        (
            {
                "type": "subagent.event",
                "stream_id": "s1",
                "event": {"type": "text.delta", "text": "x"},
            },
            "subagent.event",
        ),
        ({"type": "stream.gap", "resync": "history"}, "stream.gap"),
    ]
    for data, expected in cases:
        model = parse_wire_event(data)
        assert model is not None, data
        assert model.type == expected


def test_parse_unknown_type_returns_none():
    assert parse_wire_event({"type": "never.heard.of"}) is None


def test_parse_missing_type_returns_none():
    assert parse_wire_event({"nope": 1}) is None


def test_models_carry_fields():
    m = parse_wire_event({"type": "tool.call", "id": "c1", "name": "bash", "args": {}})
    assert isinstance(m, ToolCall)
    assert m.name == "bash"
    ask = parse_wire_event(
        {
            "type": "ask.pending",
            "id": "a1",
            "kind": "plan",
            "payload": {"summary": "s"},
            "created": "now",
        }
    )
    assert isinstance(ask, AskPending)
    assert ask.kind == "plan"
    done = parse_wire_event({"type": "turn.finished", "turn_id": "t", "interrupted": True})
    assert isinstance(done, TurnFinished)
    assert done.interrupted is True
    delta = parse_wire_event({"type": "text.delta", "text": "x"})
    assert isinstance(delta, TextDelta)
