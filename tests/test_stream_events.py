"""The shared pydantic-ai stream-event -> dict mapping (used by headless
stream-json and the server's event bus)."""

from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPart,
    TextPartDelta,
    ThinkingPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
)

from marim_harness.stream_events import event_to_dict


def test_text_part_start_and_delta():
    start = PartStartEvent(index=0, part=TextPart(content="hi"))
    delta = PartDeltaEvent(index=0, delta=TextPartDelta(content_delta=" there"))
    assert event_to_dict(start) == {"type": "text", "text": "hi"}
    assert event_to_dict(delta) == {"type": "text", "text": " there"}


def test_thinking_part_start_and_delta():
    start = PartStartEvent(index=0, part=ThinkingPart(content="hmm"))
    delta = PartDeltaEvent(index=0, delta=ThinkingPartDelta(content_delta="..."))
    assert event_to_dict(start) == {"type": "thinking", "text": "hmm"}
    assert event_to_dict(delta) == {"type": "thinking", "text": "..."}


def test_tool_call_event():
    event = FunctionToolCallEvent(
        part=ToolCallPart(tool_name="read_file", args={"path": "a.txt"}, tool_call_id="tc-1")
    )
    assert event_to_dict(event) == {
        "type": "tool_call",
        "name": "read_file",
        "args": {"path": "a.txt"},
        "id": "tc-1",
    }


def test_tool_result_event():
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content="foo", tool_call_id="tc-1")
    )
    obj = event_to_dict(event)
    assert obj is not None
    assert obj["type"] == "tool_result"
    assert obj["id"] == "tc-1"
    assert obj["content"] == "foo"


def test_unmapped_event_returns_none():
    class Unknown:
        pass

    assert event_to_dict(Unknown()) is None
