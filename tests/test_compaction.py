from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.compaction import compact_history, estimate_tokens


def _round(n: int, content_size: int = 40) -> list:
    """One realistic user turn: prompt -> tool call -> tool return -> answer."""
    filler = "x" * content_size
    tid = f"t{n}"
    return [
        ModelRequest(parts=[UserPromptPart(content=f"prompt {n} {filler}")]),
        ModelResponse(
            parts=[
                TextPart(content=f"thinking {n} {filler}"),
                ToolCallPart(
                    tool_name="read_file",
                    args={"path": f"file{n}.py"},
                    tool_call_id=tid,
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read_file",
                    content=f"contents {n} {filler}",
                    tool_call_id=tid,
                )
            ]
        ),
        ModelResponse(parts=[TextPart(content=f"answer {n} {filler}")]),
    ]


def _history(rounds: int, content_size: int = 40) -> list:
    out: list = []
    for n in range(rounds):
        out.extend(_round(n, content_size))
    return out


def _tool_returns_are_paired(history: list) -> bool:
    """Every ToolReturnPart must have a preceding ToolCallPart with the same id."""
    seen_calls: set[str] = set()
    for msg in history:
        for part in msg.parts:
            if isinstance(part, ToolCallPart):
                seen_calls.add(part.tool_call_id)
            elif isinstance(part, ToolReturnPart):
                if part.tool_call_id not in seen_calls:
                    return False
    return True


def test_estimate_tokens_grows_with_content():
    small = _history(2, content_size=10)
    big = _history(2, content_size=400)
    assert estimate_tokens(big) > estimate_tokens(small)
    assert estimate_tokens([]) == 0


def test_no_compaction_under_threshold():
    history = _history(3)
    result, did = compact_history(history, max_tokens=1_000_000)
    assert did is False
    assert result is history


def test_no_compaction_when_history_is_short():
    history = _history(1)  # 4 messages, nothing meaningful to drop
    result, did = compact_history(history, max_tokens=1, keep_last_messages=20)
    assert did is False
    assert result is history


def test_compacts_when_over_threshold():
    history = _history(20)  # 80 messages
    result, did = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    assert len(result) < len(history)


def test_head_is_preserved():
    history = _history(20)
    result, did = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    assert result[0] is history[0]  # original task anchor kept


def test_tail_starts_at_a_user_turn_and_pairs_tool_returns():
    history = _history(20)
    result, did = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    # the message right after the head must open a fresh user turn
    tail_start = result[1]
    assert isinstance(tail_start, ModelRequest)
    assert any(isinstance(p, UserPromptPart) for p in tail_start.parts)
    # and the result must never orphan a tool return
    assert _tool_returns_are_paired(result)


def test_keeps_roughly_the_last_messages():
    history = _history(20)
    result, did = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    # the final answer is still there
    assert result[-1] is history[-1]
