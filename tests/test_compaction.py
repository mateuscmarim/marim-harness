import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.compaction import (
    MASKED_OBSERVATION,
    SUMMARY_PREFIX,
    CompactionBreaker,
    compact_history,
    compact_history_with_summary,
    estimate_tokens,
    mask_stale_observations,
    render_transcript,
    summary_text,
    will_compact,
)


def test_summary_text_extracts_body_from_summary_message():
    content = f"{SUMMARY_PREFIX}\n\nWe discussed the parser and fixed a bug."
    assert summary_text(content) == "We discussed the parser and fixed a bug."


def test_summary_text_none_for_non_summary_and_bad_input():
    assert summary_text("just a normal prompt") is None
    assert summary_text(["look", {"kind": "binary"}]) is None  # list content
    assert summary_text(123) is None  # non-str
    assert summary_text(SUMMARY_PREFIX) is None  # prefix only, no body


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
            elif isinstance(part, ToolReturnPart) and part.tool_call_id not in seen_calls:
                return False
    return True


def test_estimate_tokens_grows_with_content():
    small = _history(2, content_size=10)
    big = _history(2, content_size=400)
    assert estimate_tokens(big) > estimate_tokens(small)
    assert estimate_tokens([]) == 0


def test_estimate_tokens_does_not_count_image_bytes_as_text():
    from pydantic_ai.messages import BinaryContent

    big_image = BinaryContent(data=b"\x89PNG" + b"\x00" * 500_000,
                              media_type="image/png")
    hist = [ModelRequest(parts=[UserPromptPart(content=["look at this", big_image])])]
    est = estimate_tokens(hist)
    # A ~500KB image must not be counted as ~500k text tokens; it contributes a
    # small flat nominal cost plus the accompanying text.
    assert est < 5000
    assert est > estimate_tokens(
        [ModelRequest(parts=[UserPromptPart(content="look at this")])]
    )


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


@pytest.mark.parametrize(
    "n_rounds, max_tokens, keep_last",
    [
        (3, 1_000_000, 20),  # under threshold -> no compaction
        (1, 1, 20),          # too short to drop anything
        (20, 1, 8),          # over threshold -> compacts
    ],
)
def test_will_compact_matches_compact_history_decision(n_rounds, max_tokens, keep_last):
    """The predicate that gates the pre-compaction hook must agree exactly with
    whether compact_history actually compacts — otherwise the hook could fire on
    a turn that doesn't compact (or stay silent on one that does)."""
    history = _history(n_rounds)
    _, did = compact_history(history, max_tokens=max_tokens, keep_last_messages=keep_last)
    assert will_compact(history, max_tokens=max_tokens, keep_last_messages=keep_last) is did


def test_measured_tokens_trigger_compaction_the_estimate_would_miss():
    """The gate prefers the provider's real last-request input-token count over the
    char/4 estimate, which undershoots dense code/JSON by ~25%. A history whose
    estimate is under budget must still compact when the measured count overflows."""
    from marim_harness.compaction import _plan_tail_start

    history = _history(6, content_size=20)  # small enough that the estimate fits
    budget = estimate_tokens(history) + 500  # estimate alone: comfortably under
    # No measurement -> estimate governs -> nothing to compact.
    assert _plan_tail_start(history, budget, keep_last_messages=4) is None
    # A real measured count above the budget -> compaction is planned.
    assert (
        _plan_tail_start(
            history, budget, keep_last_messages=4, measured_tokens=budget + 1
        )
        is not None
    )


def test_measured_tokens_below_estimate_does_not_lower_the_gate():
    """max(estimate, measured): a measured count SMALLER than the estimate (e.g. the
    history grew since the request) must never mask an over-budget estimate."""
    from marim_harness.compaction import _plan_tail_start

    history = _history(20)
    # estimate is well over a tiny budget; a small measured count must not rescue it.
    assert (
        _plan_tail_start(history, 1, keep_last_messages=8, measured_tokens=0)
        is not None
    )


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


# --- Phase 2: summarization -------------------------------------------------


def _summarizer(text: str = "SUMMARY", record: list | None = None):
    async def summarize(messages: list) -> str:
        if record is not None:
            record.extend(messages)
        return text

    return summarize


def test_render_transcript_includes_roles_and_tools():
    text = render_transcript(_round(1))
    assert "prompt 1" in text
    assert "read_file" in text
    assert "contents 1" in text
    assert "answer 1" in text


@pytest.mark.anyio
async def test_summary_message_inserted_between_head_and_tail():
    history = _history(20)
    result, did = await compact_history_with_summary(
        history, max_tokens=1, summarizer=_summarizer("RECAP"), keep_last_messages=8
    )
    assert did is True
    assert result[0] is history[0]  # head preserved
    note = result[1]
    assert isinstance(note, ModelRequest)
    prompts = [p for p in note.parts if isinstance(p, UserPromptPart)]
    assert prompts and "RECAP" in prompts[0].content
    assert result[-1] is history[-1]  # tail preserved
    assert _tool_returns_are_paired(result)


@pytest.mark.anyio
async def test_summarizer_receives_the_dropped_middle():
    history = _history(20)
    got: list = []
    result, did = await compact_history_with_summary(
        history, max_tokens=1, summarizer=_summarizer("X", record=got),
        keep_last_messages=8,
    )
    assert did is True
    assert got  # the middle was handed to the summarizer
    assert history[0] not in got  # head excluded
    assert history[-1] not in got  # tail excluded


@pytest.mark.anyio
async def test_summary_failure_falls_back_to_truncation():
    history = _history(20)

    async def boom(messages: list) -> str:
        raise RuntimeError("summary model down")

    result, did = await compact_history_with_summary(
        history, max_tokens=1, summarizer=boom, keep_last_messages=8
    )
    truncated, _ = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    assert result == truncated  # no synthetic note inserted


@pytest.mark.anyio
async def test_empty_summary_falls_back_to_truncation():
    history = _history(20)
    result, did = await compact_history_with_summary(
        history, max_tokens=1, summarizer=_summarizer(""), keep_last_messages=8
    )
    truncated, _ = compact_history(history, max_tokens=1, keep_last_messages=8)
    assert did is True
    assert result == truncated


@pytest.mark.anyio
async def test_no_summary_under_threshold():
    history = _history(3)
    called: list = []

    async def rec(messages: list) -> str:
        called.append(messages)
        return "x"

    result, did = await compact_history_with_summary(
        history, max_tokens=1_000_000, summarizer=rec
    )
    assert did is False
    assert result is history
    assert called == []  # never paid for a summary we didn't need


def test_summarize_prompt_frames_transcript_with_explicit_instruction():
    from marim_harness.compaction import _summarize_prompt

    p = _summarize_prompt("User: hi\nAssistant: hello")
    assert "User: hi" in p and "Assistant: hello" in p  # the transcript is included
    assert "ummariz" in p  # an explicit in-message summarize instruction
    assert p.rstrip().endswith("Summary:")  # cues the model to emit the summary
    # tells the model not to reply conversationally (the weak-model failure mode)
    assert "only the summary" in p.lower() or "do not reply" in p.lower()


@pytest.mark.anyio
async def test_make_summarizer_sends_framed_prompt_to_model():
    from pydantic_ai.models.function import AgentInfo, FunctionModel

    from marim_harness.compaction import make_summarizer

    seen: dict = {}

    def fn(messages: list, info: AgentInfo) -> ModelResponse:
        seen["prompt"] = str(getattr(messages[-1].parts[-1], "content", ""))
        return ModelResponse(parts=[TextPart(content="ok")])

    summarize = make_summarizer(FunctionModel(fn))
    out = await summarize([ModelRequest(parts=[UserPromptPart(content="explain this")])])
    assert out == "ok"
    assert "explain this" in seen["prompt"]  # the transcript reached the model
    assert "ummariz" in seen["prompt"]  # wrapped with the explicit framing


def _tool_return(tid: str, content) -> ModelRequest:
    return ModelRequest(
        parts=[ToolReturnPart(tool_name="read_file", content=content, tool_call_id=tid)]
    )


def test_mask_keeps_recent_returns_and_elides_older_bulky_ones():
    big = "x" * 500
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        _tool_return("t1", f"old {big}"),
        _tool_return("t2", f"mid {big}"),
        _tool_return("t3", f"recent {big}"),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    new_history, masked = mask_stale_observations(history, keep_recent=1)

    assert masked == 2  # t1 and t2 elided; t3 (most recent) kept
    contents = [
        p.content
        for m in new_history
        for p in m.parts
        if isinstance(p, ToolReturnPart)
    ]
    assert contents[0] == MASKED_OBSERVATION
    assert contents[1] == MASKED_OBSERVATION
    assert contents[2].startswith("recent ")


def test_mask_preserves_pairing_identity():
    big = "y" * 500
    history = [_tool_return("call-abc", big), _tool_return("call-def", big)]
    new_history, masked = mask_stale_observations(history, keep_recent=0)

    ids = [p.tool_call_id for m in new_history for p in m.parts]
    names = [p.tool_name for m in new_history for p in m.parts]
    assert masked == 2
    assert ids == ["call-abc", "call-def"]  # tool_call_id untouched
    assert names == ["read_file", "read_file"]  # tool_name untouched


def test_mask_skips_small_returns():
    history = [_tool_return("t1", "ok"), _tool_return("t2", "z" * 500)]
    _, masked = mask_stale_observations(history, keep_recent=0, min_chars=200)
    assert masked == 1  # only the bulky one


def test_mask_does_not_mutate_input_and_is_idempotent():
    big = "w" * 500
    history = [_tool_return("t1", big)]
    new_history, masked = mask_stale_observations(history, keep_recent=0)
    assert masked == 1
    assert history[0].parts[0].content == big  # original untouched

    # Re-running over already-masked history is a no-op.
    _, again = mask_stale_observations(new_history, keep_recent=0)
    assert again == 0


# --- CompactionBreaker (rapid-refill circuit breaker) -------------------------


def test_breaker_trips_after_three_rapid_refills():
    b = CompactionBreaker()
    b.note_compact()                    # first compaction: baseline, not rapid
    for _ in range(3):                  # three refill-compactions within 3 turns each
        b.note_turn()
        b.note_compact()
    assert b.open


def test_breaker_slow_refill_resets_the_streak():
    b = CompactionBreaker()
    b.note_compact()
    b.note_turn()
    b.note_compact()                   # rapid #1
    b.note_turn()
    b.note_compact()                   # rapid #2
    for _ in range(4):                  # 4 turns > rapid_turns → streak broken
        b.note_turn()
    b.note_compact()
    assert not b.open
    assert b.consecutive_rapid_refills == 0


def test_breaker_reset_clears_everything():
    b = CompactionBreaker()
    b.note_compact()
    for _ in range(3):
        b.note_turn()
        b.note_compact()
    assert b.open
    b.reset()
    assert not b.open
    assert b.turns_since_compact is None


def test_breaker_ignores_turns_before_first_compact():
    b = CompactionBreaker()
    for _ in range(10):
        b.note_turn()
    b.note_compact()
    assert b.consecutive_rapid_refills == 0
    assert not b.open
