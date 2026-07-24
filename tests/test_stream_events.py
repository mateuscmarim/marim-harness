"""The shared pydantic-ai stream-event -> dict mapping (used by headless
stream-json and the server's event bus)."""

from pydantic_ai.messages import (
    BinaryContent,
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


def test_tool_result_structured_content_is_json_not_repr():
    # A structured (list/dict) tool return must be serialized as JSON, not str()-ified
    # into a Python repr (single-quoted, unparseable) that leaks into the event stream.
    import json

    event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="grep", content=[{"path": "a.py"}, {"path": "b.py"}], tool_call_id="t2"
        )
    )
    obj = event_to_dict(event)
    assert obj is not None
    # Valid JSON round-trips; the old str() output "[{'path': 'a.py'}, ...]" would not.
    assert json.loads(obj["content"]) == [{"path": "a.py"}, {"path": "b.py"}]
    assert "'" not in obj["content"]


def test_tool_result_plain_string_content_preserved():
    # Plain-string content is passed through untouched (no JSON quoting).
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content="PORT = 8080", tool_call_id="t1")
    )
    assert event_to_dict(event)["content"] == "PORT = 8080"


def test_tool_result_scalar_binary_content_is_placeholder_not_bytes():
    # A read_file image return must not dump str(BinaryContent) (the full base64
    # body, up to ~20MB) into the headless/WebSocket JSON event stream.
    img = BinaryContent(data=b"x" * 4096, media_type="image/png")
    event = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t3")
    )
    obj = event_to_dict(event)
    assert obj is not None
    assert obj["content"] == "[image image/png, 4 KB]"
    assert "x" * 100 not in obj["content"]


def test_tool_result_list_with_binary_content_is_placeholder_not_bytes():
    # Same guard for a list of content blocks (e.g. an MCP tool returning mixed
    # text + image content) — the binary item must not reach json.dumps(default=str).
    img = BinaryContent(data=b"y" * 4096, media_type="image/jpeg")
    event = FunctionToolResultEvent(
        part=ToolReturnPart(
            tool_name="mcp_tool", content=["caption", img], tool_call_id="t4"
        )
    )
    obj = event_to_dict(event)
    assert obj is not None
    assert "[image image/jpeg, 4 KB]" in obj["content"]
    assert "caption" in obj["content"]
    assert "y" * 100 not in obj["content"]


def test_unmapped_event_returns_none():
    class Unknown:
        pass

    assert event_to_dict(Unknown()) is None
