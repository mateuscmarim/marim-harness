from marim_harness.server.wire_events import (
    AskPending,
    AskResolved,
    TextDelta,
    ToolCall,
    ToolResult,
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
        ({"type": "turn.usage", "turn_id": "t1", "total_tokens": 42}, "turn.usage"),
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
            {"type": "session.renamed", "from": "old", "to": "new"},
            "session.renamed",
        ),
        ({"type": "tasks.changed"}, "tasks.changed"),
        ({"type": "jobs.changed"}, "jobs.changed"),
        ({"type": "compaction.started"}, "compaction.started"),
        (
            {"type": "compaction.finished", "before": 1000, "after": 500},
            "compaction.finished",
        ),
        (
            {
                "type": "subagent.event",
                "stream_id": "s1",
                "event": {"type": "text.delta", "text": "x"},
            },
            "subagent.event",
        ),
        (
            {"type": "subagent.notice", "stream_id": "s1", "message": "started"},
            "subagent.notice",
        ),
        (
            {"type": "subagent.model", "stream_id": "s1", "model": "gpt-4"},
            "subagent.model",
        ),
        (
            {"type": "subagent.thinking", "stream_id": "s1", "level": "high"},
            "subagent.thinking",
        ),
        (
            {"type": "subagent.usage", "stream_id": "s1", "usage": {"input": 10}},
            "subagent.usage",
        ),
        (
            {
                "type": "workflow.spawned",
                "stream_id": "w1",
                "spawn_type": "agent",
                "task": "research",
                "parent_tool_call_id": "tc1",
            },
            "workflow.spawned",
        ),
        (
            {
                "type": "workflow.started",
                "tool_call_id": "tc1",
                "title": "Research Phase",
            },
            "workflow.started",
        ),
        (
            {
                "type": "workflow.logged",
                "tool_call_id": "tc1",
                "message": "Running search",
            },
            "workflow.logged",
        ),
        (
            {
                "type": "workflow.finished",
                "tool_call_id": "tc1",
                "outcome": "success",
                "failed": False,
            },
            "workflow.finished",
        ),
        (
            {"type": "workflow.spawn_finished", "stream_id": "w1", "report": "done"},
            "workflow.spawn_finished",
        ),
        ({"type": "session.ttft", "seconds": 0.5}, "session.ttft"),
        ({"type": "session.mode_changed", "mode": "auto"}, "session.mode_changed"),
        ({"type": "session.notice", "message": "connected"}, "session.notice"),
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


def test_ask_resolved_accepts_answer_shape():
    """AskResolved parses the normal answer-submission shape."""
    m = parse_wire_event({"type": "ask.resolved", "id": "a1", "answer": {"ok": True}})
    assert isinstance(m, AskResolved)
    assert m.id == "a1"
    assert m.answer == {"ok": True}
    assert m.cancelled is False
    assert m.reason is None


def test_ask_resolved_accepts_cancelled_shape():
    """AskResolved parses the interrupted/cancelled shape from host._cancel_pending."""
    m = parse_wire_event(
        {"type": "ask.resolved", "id": "a1", "cancelled": True, "reason": "interrupted"}
    )
    assert isinstance(m, AskResolved)
    assert m.id == "a1"
    assert m.cancelled is True
    assert m.reason == "interrupted"
    assert m.answer is None


def test_tool_result_status_matches_the_host_publish_shape():
    """A client rendering from the wire alone has no ``ToolReturnPart`` to read a
    denied/failed call off, so the outcome has to ride on ``tool.result``. Pin
    the producer shape: the payload ``SessionHost`` publishes (``event_to_dict``
    remapped through ``STREAM_EVENT_TYPES``) round-trips into ``ToolResult`` with
    the status intact — and an older server that omits the field still parses, as
    a plain success."""
    from pydantic_ai.messages import FunctionToolResultEvent, ToolReturnPart

    from marim_harness.server.schema import STREAM_EVENT_TYPES
    from marim_harness.stream_events import event_to_dict

    for outcome, expected in (("success", "done"), ("failed", "failed"), ("denied", "denied")):
        event = FunctionToolResultEvent(
            part=ToolReturnPart(
                tool_name="write_file",
                content="x",
                tool_call_id="c1",
                outcome=outcome,  # pyright: ignore[reportArgumentType]
            )
        )
        obj = event_to_dict(event)
        assert obj is not None
        model = parse_wire_event({"type": STREAM_EVENT_TYPES[str(obj.pop("type"))], **obj})
        assert isinstance(model, ToolResult)
        assert model.status == expected

    legacy = parse_wire_event({"type": "tool.result", "id": "c1", "content": "ok"})
    assert isinstance(legacy, ToolResult)
    assert legacy.status == "done"


def test_session_renamed_from_field():
    """SessionRenamed parses the 'from' keyword correctly via Field alias."""
    m = parse_wire_event({"type": "session.renamed", "from": "session-a", "to": "session-b"})
    from marim_harness.server.wire_events import SessionRenamed

    assert isinstance(m, SessionRenamed)
    assert m.from_ == "session-a"
    assert m.to == "session-b"
