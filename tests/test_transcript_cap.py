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
