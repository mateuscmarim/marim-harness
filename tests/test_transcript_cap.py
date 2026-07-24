from datetime import datetime, timezone

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolReturnPart,
)

from marim_harness.workspace.agents import cap_transcript


def _ret(content: str) -> ModelRequest:
    return ModelRequest(parts=[ToolReturnPart(
        tool_name="read_file", content=content, tool_call_id="t1",
        timestamp=datetime.now(tz=timezone.utc),
    )])


def test_cap_truncates_only_oversized_tool_results():
    big = "x" * 5000
    msgs = [_ret(big), ModelResponse(parts=[TextPart(content="all good")])]
    out = cap_transcript(msgs, cap=2000)
    ret = out[0].parts[0]
    assert len(str(ret.content)) < 2100          # head + marker, well under original
    assert "truncated, 5000 chars" in str(ret.content)
    # Non-tool parts are untouched.
    assert out[1].parts[0].content == "all good"


def test_cap_leaves_small_results_intact():
    msgs = [_ret("short output")]
    out = cap_transcript(msgs, cap=2000)
    assert out[0].parts[0].content == "short output"


def test_cap_handles_non_string_content():
    # A list/blocks content must not crash; it is stringified for length checks.
    part = ToolReturnPart(tool_name="x", content=[{"type": "text", "text": "y" * 5000}],
                          tool_call_id="t", timestamp=datetime.now(tz=timezone.utc))
    out = cap_transcript([ModelRequest(parts=[part])], cap=100)
    assert "truncated" in str(out[0].parts[0].content)


def test_cap_leaves_binary_tool_returns_intact():
    """A binary tool return (a read_file image) must pass through untouched:
    the generic non-string branch would str() it — a bytes repr ~2.5x the
    image size — clipping it to garbage AND bloating the checkpoint. Size for
    binary content is the sidecar externalization's job, not the cap's."""
    from pydantic_ai.messages import BinaryContent, ToolReturnPart

    img = BinaryContent(data=b"\x89PNG\r\n\x1a\n" + b"p" * 4096, media_type="image/png")
    msg = ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="c1"),
    ])
    out = cap_transcript([msg], cap=100)
    assert out[0].parts[0].content is img
    # A list return carrying a binary item is protected the same way.
    lst = ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=[img, "note"], tool_call_id="c2"),
    ])
    out = cap_transcript([lst], cap=100)
    assert out[0].parts[0].content == [img, "note"]
