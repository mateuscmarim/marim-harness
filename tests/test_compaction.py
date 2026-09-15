from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ThinkingPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.compaction import (
    ELIDED_POINTER_PREFIX,
    MASKED_OBSERVATION,
    SUMMARY_PREFIX,
    UPSTREAM_SUMMARY_PREFIX,
    CompactionBreaker,
    _elided_pointer,
    _measured_or_estimated,
    elided_pointer_path,
    estimate_tokens,
    render_transcript,
    repair_masked_narrowed_returns,
    revalidate_elided_pointers,
    summary_text,
)
from marim_harness.tools.impl.offload import OFFLOAD_GONE_NOTE


def _tool_return(call_id: str, content: object) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content=content, tool_call_id=call_id)]
    )


def test_estimate_tokens_wraps_upstream_estimator():
    from pydantic_ai_harness.compaction import estimate_token_count

    history = [ModelRequest(parts=[UserPromptPart(content="hello " * 100)])]
    assert estimate_tokens(history) == estimate_token_count(history)
    assert estimate_tokens([]) == 0


def test_estimate_tokens_uses_upstream_binary_behavior():
    image = BinaryContent(data=b"x" * 500_000, media_type="image/png")
    history = [ModelRequest(parts=[UserPromptPart(content=["look", image])])]
    assert estimate_tokens(history) < 100


def test_measured_tokens_remain_a_floor_over_upstream_estimate():
    history = [ModelRequest(parts=[UserPromptPart(content="x" * 400)])]
    estimated = estimate_tokens(history)
    assert _measured_or_estimated(history, None) == estimated
    assert _measured_or_estimated(history, estimated - 1) == estimated
    assert _measured_or_estimated(history, estimated + 1) == estimated + 1


def test_summary_text_reads_historical_and_upstream_summaries():
    assert summary_text(f"{SUMMARY_PREFIX}\n\nHistorical recap") == "Historical recap"
    assert summary_text(f"{UPSTREAM_SUMMARY_PREFIX}Upstream recap") == "Upstream recap"


def test_summary_text_rejects_normal_or_empty_content():
    assert summary_text("ordinary user prompt") is None
    assert summary_text(SUMMARY_PREFIX) is None
    assert summary_text(["structured"]) is None


def test_render_transcript_includes_roles_and_tools():
    history = [
        ModelRequest(parts=[UserPromptPart(content="inspect it")]),
        ModelResponse(
            parts=[
                ThinkingPart(content="need a read"),
                ToolCallPart(tool_name="read_file", args={"path": "a.py"}, tool_call_id="1"),
            ]
        ),
        _tool_return("1", "contents"),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    transcript = render_transcript(history)
    for expected in ("inspect it", "need a read", "read_file", "contents", "done"):
        assert expected in transcript


def test_render_transcript_hides_binary_bytes():
    image = BinaryContent(data=b"\x89PNGbytes", media_type="image/png")
    transcript = render_transcript([_tool_return("1", ["caption", image])])
    assert "[image image/png" in transcript
    assert "PNGbytes" not in transcript


def test_elided_pointer_path_round_trips_and_rejects_other_content():
    path = "/pad/elided/1.txt"
    assert elided_pointer_path(_elided_pointer(path)) == path
    assert elided_pointer_path(MASKED_OBSERVATION) is None
    assert elided_pointer_path(f"{ELIDED_POINTER_PREFIX}unfinished") is None


def test_revalidate_rewrites_dangling_pointer_without_mutating_input():
    history = [_tool_return("1", _elided_pointer("/pad/gone.txt"))]
    rewritten, count = revalidate_elided_pointers(history, exists=lambda _: False)
    assert count == 1
    assert rewritten is not history
    assert rewritten[0].parts[0].content == MASKED_OBSERVATION
    assert history[0].parts[0].content != MASKED_OBSERVATION


def test_revalidate_live_pointer_is_same_object_and_idempotent():
    history = [_tool_return("1", _elided_pointer("/pad/live.txt"))]
    unchanged, count = revalidate_elided_pointers(history, exists=lambda _: True)
    assert unchanged is history
    assert count == 0
    once, first_count = revalidate_elided_pointers(history, exists=lambda _: False)
    twice, second_count = revalidate_elided_pointers(once, exists=lambda _: False)
    assert first_count == 1
    assert second_count == 0
    assert twice is once


def test_revalidate_annotates_dangling_saved_output_handle():
    content = "preview\n\n[full output saved to `/pad/gone.txt` — use read_file]"
    history = [_tool_return("1", content)]
    rewritten, count = revalidate_elided_pointers(history, exists=lambda _: False)
    assert count == 1
    assert rewritten[0].parts[0].content == content + OFFLOAD_GONE_NOTE


def test_revalidate_resolves_relative_saved_handle_against_base(tmp_path):
    live = tmp_path / "saved.txt"
    live.write_text("full")
    content = "preview\n\n[full output saved to `saved.txt` — use read_file]"
    history = [_tool_return("1", content)]
    unchanged, count = revalidate_elided_pointers(history, base=tmp_path)
    assert unchanged is history
    assert count == 0


def test_repair_strips_tool_kind_only_from_historical_masked_returns():
    raw = [
        {
            "parts": [
                {"tool_kind": "tool-search", "content": MASKED_OBSERVATION},
                {"tool_kind": "tool-search", "content": {"discovered_tools": []}},
            ]
        }
    ]
    assert repair_masked_narrowed_returns(raw) == 1
    assert "tool_kind" not in raw[0]["parts"][0]
    assert raw[0]["parts"][1]["tool_kind"] == "tool-search"


def test_repair_tolerates_malformed_raw_input():
    assert repair_masked_narrowed_returns(None) == 0
    assert repair_masked_narrowed_returns(["bad", {}, {"parts": None}]) == 0


def test_breaker_trips_after_three_rapid_refills():
    breaker = CompactionBreaker()
    breaker.note_compact()
    for _ in range(3):
        breaker.note_turn()
        breaker.note_compact()
    assert breaker.open


def test_breaker_slow_refill_resets_streak_and_reset_clears_state():
    breaker = CompactionBreaker(rapid_turns=1)
    breaker.note_compact()
    breaker.note_turn()
    breaker.note_compact()
    assert breaker.consecutive_rapid_refills == 1
    breaker.note_turn()
    breaker.note_turn()
    breaker.note_compact()
    assert breaker.consecutive_rapid_refills == 0
    breaker.reset()
    assert breaker.turns_since_compact is None
    assert not breaker.open
