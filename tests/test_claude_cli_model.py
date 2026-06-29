
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from marim_harness.config.claude_cli_model import (
    DoneChunk,
    TextChunk,
    consume_cli_stream,
    extract_system,
    flatten_history,
    format_activity_line,
    latest_user_text,
    permission_mode_for,
    request_usage_from_cli,
)


def test_permission_mode_mapping():
    assert permission_mode_for("auto") == "acceptEdits"
    assert permission_mode_for("ask") == "acceptEdits"
    assert permission_mode_for("plan") == "plan"
    # Unknown falls back to the safe read-only plan mode.
    assert permission_mode_for("weird") == "plan"


def test_latest_user_text_takes_newest_request():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="first")]),
        ModelResponse(parts=[TextPart(content="answer one")]),
        ModelRequest(parts=[UserPromptPart(content="second")]),
    ]
    assert latest_user_text(msgs) == "second"


def test_latest_user_text_joins_list_content():
    msgs = [ModelRequest(parts=[UserPromptPart(content=["a", "b"])])]
    assert latest_user_text(msgs) == "a\nb"


def test_extract_system_prefers_instructions_then_system_parts():
    msgs = [
        ModelRequest(
            parts=[SystemPromptPart(content="sys-part")],
            instructions="the-instructions",
        ),
    ]
    assert extract_system(msgs) == "the-instructions"
    msgs2 = [ModelRequest(parts=[SystemPromptPart(content="sys-only")])]
    assert extract_system(msgs2) == "sys-only"


def test_flatten_history_labels_roles():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="hello")]),
        ModelResponse(parts=[TextPart(content="hi there")]),
        ModelRequest(parts=[UserPromptPart(content="more")]),
    ]
    out = flatten_history(msgs)
    assert "User: hello" in out
    assert "Assistant: hi there" in out
    assert out.rstrip().endswith("User: more")


def test_request_usage_folds_cache_and_cost():
    u = request_usage_from_cli(
        {
            "input_tokens": 10,
            "output_tokens": 5,
            "cache_read_input_tokens": 100,
            "cache_creation_input_tokens": 7,
        },
        total_cost_usd=0.25,
    )
    assert u.input_tokens == 117  # 10 + 100 + 7, inclusive of cache
    assert u.output_tokens == 5
    assert u.cache_read_tokens == 100
    assert u.cache_write_tokens == 7
    from marim_harness.usage import COST_DETAIL_KEY

    assert u.details[COST_DETAIL_KEY] == 250_000


def test_format_activity_line_summarizes_common_tools():
    assert format_activity_line("Read", {"file_path": "a/b.py"}) == "⏺ Read a/b.py"
    assert format_activity_line("Bash", {"command": "ls -la"}) == "⏺ Bash ls -la"
    assert format_activity_line("Grep", {"pattern": "foo"}) == "⏺ Grep foo"
    # Unknown tool: name only, no crash.
    assert format_activity_line("TodoWrite", {"todos": []}) == "⏺ TodoWrite"


async def _collect(objs):
    async def gen():
        for o in objs:
            yield o

    out = []
    async for chunk in consume_cli_stream(gen()):
        out.append(chunk)
    return out


@pytest.mark.anyio
async def test_consume_streams_text_activity_and_done():
    objs = [
        {"type": "system", "subtype": "init", "session_id": "sess-9", "model": "claude-x"},
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Looking…"}]},
        },
        {
            "type": "assistant",
            "message": {
                "content": [
                    {"type": "tool_use", "id": "t1", "name": "Read", "input": {"file_path": "x.py"}}
                ]
            },
        },
        {
            "type": "assistant",
            "message": {"content": [{"type": "text", "text": "Done."}]},
        },
        {
            "type": "result",
            "result": "Done.",
            "session_id": "sess-9",
            "num_turns": 2,
            "usage": {"input_tokens": 3, "output_tokens": 4},
            "total_cost_usd": 0.01,
        },
    ]
    chunks = await _collect(objs)
    texts = [c.delta for c in chunks if isinstance(c, TextChunk)]
    assert texts[0] == "Looking…"
    assert "⏺ Read x.py" in "".join(texts)
    assert texts[-1] == "Done."
    done = chunks[-1]
    assert isinstance(done, DoneChunk)
    assert done.session_id == "sess-9"
    assert done.complete is True
    assert done.usage.output_tokens == 4
    # Final text is the concatenation of everything streamed.
    assert done.text == "".join(texts)


@pytest.mark.anyio
async def test_consume_marks_incomplete_when_no_result():
    objs = [{"type": "assistant", "message": {"content": [{"type": "text", "text": "hi"}]}}]
    chunks = await _collect(objs)
    done = chunks[-1]
    assert isinstance(done, DoneChunk)
    assert done.complete is False
    assert done.text == "hi"
