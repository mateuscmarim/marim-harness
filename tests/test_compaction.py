import pytest
from pydantic_ai.messages import (
    BinaryContent,
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)

from marim_harness.compaction import (
    _SUMMARY_INSTRUCTIONS,
    ELIDED_POINTER_PREFIX,
    MASKED_OBSERVATION,
    SUMMARY_PREFIX,
    CompactionBreaker,
    _elided_pointer,
    _summarize_prompt,
    compact_history,
    compact_history_with_summary,
    elided_pointer_path,
    estimate_tokens,
    mask_stale_observations,
    render_transcript,
    revalidate_elided_pointers,
    summary_text,
    will_compact,
)
from marim_harness.tools.impl.offload import OFFLOAD_GONE_NOTE


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


def test_will_compact_gates_on_measured_tokens_like_maybe_compact():
    """will_compact must accept the provider's measured input-token count so a
    pre-compaction caller reaches the same verdict maybe_compact will: a history
    the char/4 estimate says fits but the provider reported as huge WILL compact,
    and will_compact must say so when handed the measurement."""
    history = [
        ModelRequest(parts=[UserPromptPart(content=f"{i}" * 400)]) for i in range(3)
    ]
    assert estimate_tokens(history) <= 1000
    assert will_compact(history, 1000, keep_last_messages=1) is False
    assert (
        will_compact(history, 1000, keep_last_messages=1, measured_tokens=5000) is True
    )


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
    async def summarize(messages, instructions=None):
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

    async def boom(messages: list, instructions: str | None = None) -> str:
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

    async def rec(messages: list, instructions: str | None = None) -> str:
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
    out = await summarize(
        [ModelRequest(parts=[UserPromptPart(content="explain this")])], None
    )
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


def test_mask_persist_puts_path_in_placeholder():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(6)]
    calls: list[tuple[str, str]] = []

    def persist(content: str, tool_name: str) -> str:
        calls.append((content, tool_name))
        return f"/pad/elided/{len(calls):03d}-{tool_name}.txt"

    masked, n = mask_stale_observations(history, keep_recent=2, persist=persist)
    assert n == 4 and len(calls) == 4
    first = masked[0].parts[0]
    assert first.content.startswith(ELIDED_POINTER_PREFIX)
    assert "/pad/elided/001-" in first.content
    assert "read_file" in first.content
    # persisted content is the original payload
    assert calls[0][0] == "X" * 300


def test_mask_persist_failure_falls_back_to_plain_placeholder():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(3)]
    masked, n = mask_stale_observations(
        history, keep_recent=1, persist=lambda content, name: None
    )
    assert n == 2
    assert masked[0].parts[0].content == MASKED_OBSERVATION


def test_mask_persist_raising_falls_back_to_plain_placeholder():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(3)]

    def persist_raises(content: str, tool_name: str) -> str:
        raise OSError("disk full")

    masked, n = mask_stale_observations(
        history, keep_recent=1, persist=persist_raises
    )
    assert n == 2
    assert masked[0].parts[0].content == MASKED_OBSERVATION


def test_mask_is_idempotent_over_pointer_placeholders():
    history = [_tool_return(f"t{i}", "X" * 300) for i in range(4)]
    once, n1 = mask_stale_observations(
        history, keep_recent=1, persist=lambda c, t: "/pad/e/001-x.txt"
    )
    twice, n2 = mask_stale_observations(
        once, keep_recent=1, persist=lambda c, t: "/pad/e/002-x.txt"
    )
    assert n1 == 3 and n2 == 0
    assert [p.parts[0].content for p in twice] == [p.parts[0].content for p in once]


def test_mask_persists_model_facing_json_for_structured_returns(tmp_path):
    # Structured content must be persisted exactly as the model saw it (compact
    # JSON via model_response_str), not as the Python repr — the pointer
    # placeholder promises a faithful read_file round-trip.
    content = {"a": 1, "b": ["x", "y"], "pad": "P" * 300}
    history = [_tool_return("t1", content), _tool_return("t2", "Z" * 300)]

    def persist(payload: str, tool_name: str) -> str:
        path = tmp_path / f"{tool_name}.txt"
        path.write_text(payload)
        return str(path)

    _, n = mask_stale_observations(history, keep_recent=1, persist=persist)
    assert n == 1
    expected = history[0].parts[0].model_response_str()  # what the model read
    assert (tmp_path / "read_file.txt").read_bytes() == expected.encode()
    assert '"a":1' in expected and "'a'" not in expected  # JSON, not Python repr


def test_mask_threshold_measured_on_model_facing_rendering():
    # A structured payload whose Python repr crosses min_chars but whose
    # model-facing JSON does not: the model never paid for the repr's extra
    # quoting/spacing, so the size gate must measure the JSON.
    content = {f"k{i}": "v" for i in range(20)}
    probe = ToolReturnPart(tool_name="t", content=content, tool_call_id="p")
    assert len(str(content)) >= 200 > len(probe.model_response_str())  # setup

    history = [_tool_return("t1", content), _tool_return("t2", "Z" * 300)]
    _, masked = mask_stale_observations(history, keep_recent=0, min_chars=200)
    assert masked == 1  # only the genuinely bulky string return


class _Unrenderable:
    """Pydantic can't serialize this (model_response_str raises), but str() works."""

    def __str__(self) -> str:
        return "U" * 300


def test_mask_unrenderable_content_masks_plain_without_persisting():
    history = [_tool_return("t1", _Unrenderable()), _tool_return("t2", "Z" * 300)]
    persisted: list[str] = []

    def persist(payload: str, tool_name: str) -> str:
        persisted.append(payload)
        return "/pad/e/001-x.txt"

    masked, n = mask_stale_observations(history, keep_recent=1, persist=persist)
    assert n == 1  # a render failure never blocks masking
    assert masked[0].parts[0].content == MASKED_OBSERVATION  # plain, no pointer
    assert persisted == []  # no unfaithful repr bytes written


class _ValueErrorContent:
    """Content whose model_response_str() raises ValueError, not TypeError."""

    def model_response_str(self) -> str:
        raise ValueError("invalid serialization value")


def test_mask_value_error_from_render_also_falls_back():
    """A ValueError from model_response_str (e.g. pydantic serializer rejecting
    an invalid value) must be caught the same way as TypeError, never blocking
    the masking pass."""
    # Build a part whose model_response_str raises ValueError directly,
    # bypassing pydantic's own serialization.  We monkey-patch the method on
    # the instance to keep the test isolated.
    real_part = ToolReturnPart(
        tool_name="read_file",
        content="X" * 300,
        tool_call_id="v1",
    )
    original_mrs = real_part.model_response_str
    real_part.model_response_str = lambda: (_ for _ in ()).throw(ValueError("boom"))  # type: ignore[assignment]
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        ModelRequest(parts=[real_part]),
        ModelRequest(parts=[ToolReturnPart(
            tool_name="read_file", content="Y" * 300, tool_call_id="v2",
        )]),
    ]
    masked, n = mask_stale_observations(history, keep_recent=1)
    assert n == 1
    assert masked[1].parts[0].content == MASKED_OBSERVATION
    # Restore for any subsequent tests that might reuse the object.
    real_part.model_response_str = original_mrs  # type: ignore[assignment]


# --- revalidate_elided_pointers (dangling scratchpad pointers) ----------------


def test_elided_pointer_path_round_trips():
    path = "/pad/elided/001-read_file.txt"
    assert elided_pointer_path(_elided_pointer(path)) == path


def test_elided_pointer_path_none_for_non_pointer_content():
    assert elided_pointer_path("plain tool output") is None
    assert elided_pointer_path(MASKED_OBSERVATION) is None
    assert elided_pointer_path({"a": 1}) is None  # non-str content
    # Prefix without the read_file suffix is not a well-formed pointer.
    assert elided_pointer_path(f"{ELIDED_POINTER_PREFIX}/pad/x.txt]") is None


def test_revalidate_rewrites_dangling_pointer_to_plain_placeholder():
    pointer = _elided_pointer("/pad/e/gone.txt")
    history = [
        ModelRequest(parts=[UserPromptPart(content="go")]),
        _tool_return("t1", pointer),
        ModelResponse(parts=[TextPart(content="done")]),
    ]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 1
    part = new_history[1].parts[0]
    assert part.content == MASKED_OBSERVATION
    # Pairing identity preserved — only the payload changes.
    assert part.tool_call_id == "t1" and part.tool_name == "read_file"
    # Input never mutated.
    assert history[1].parts[0].content == pointer


def test_revalidate_leaves_live_pointer_and_returns_input_unchanged():
    history = [_tool_return("t1", _elided_pointer("/pad/e/live.txt"))]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: True)
    assert n == 0
    # Same object back: callers can detect "nothing dangled" without an
    # equality walk, so no spurious history replacement/persist churn.
    assert new_history is history


def test_revalidate_ignores_non_pointer_masked_and_small_content():
    history = [
        _tool_return("t1", "ordinary output"),
        _tool_return("t2", MASKED_OBSERVATION),
        _tool_return("t3", {"a": 1}),
    ]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 0
    assert new_history is history


def test_revalidate_is_idempotent():
    history = [_tool_return("t1", _elided_pointer("/pad/e/gone.txt"))]
    once, n1 = revalidate_elided_pointers(history, exists=lambda p: False)
    twice, n2 = revalidate_elided_pointers(once, exists=lambda p: False)
    assert n1 == 1 and n2 == 0
    assert twice is once


def test_revalidate_default_predicate_checks_the_filesystem(tmp_path):
    live = tmp_path / "live.txt"
    live.write_text("payload")
    gone = tmp_path / "gone.txt"  # never written
    history = [
        _tool_return("t1", _elided_pointer(str(live))),
        _tool_return("t2", _elided_pointer(str(gone))),
    ]
    new_history, n = revalidate_elided_pointers(history)
    assert n == 1
    assert new_history[0].parts[0].content == _elided_pointer(str(live))
    assert new_history[1].parts[0].content == MASKED_OBSERVATION


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


def test_summarize_prompt_appends_compact_instructions_block():
    prompt = _summarize_prompt("T", "focus on the auth bug")
    assert "## Compact instructions" in prompt
    assert "focus on the auth bug" in prompt
    assert "## Compact instructions" not in _summarize_prompt("T", None)


def test_summary_instructions_cover_the_structured_schema():
    for needle in (
        "Primary request and intent",
        "All user messages",
        "verbatim",
        "Next step",
        "Security-relevant",
    ):
        assert needle in _SUMMARY_INSTRUCTIONS, needle


@pytest.mark.anyio
async def test_compact_with_summary_threads_instructions_to_summarizer():
    received: list = []

    async def summarizer(messages, instructions=None):
        received.append(instructions)
        return "SUMMARY"

    history = _history(rounds=12)
    await compact_history_with_summary(
        history, max_tokens=10, summarizer=summarizer, instructions="keep the tests"
    )
    assert received == ["keep the tests"]


def test_estimate_tokens_counts_scalar_image_tool_return_flat():
    img = BinaryContent(data=b"x" * 100_000, media_type="image/png")
    msg = ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
    ])
    tokens = estimate_tokens([msg])
    assert tokens < 100_000 // 4  # flat image cost, not the bytes-repr length
    assert tokens >= 1500


def test_mask_replaces_image_tool_return_regardless_of_min_chars():
    img = BinaryContent(data=b"\x89PNG" + b"p" * 10, media_type="image/png")
    history = [
        ModelRequest(parts=[
            ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
        ]),
        ModelRequest(parts=[UserPromptPart(content="next turn")]),
    ]
    masked, count = mask_stale_observations(history, keep_recent=0, min_chars=10_000)
    assert count == 1
    assert masked[0].parts[0].content == MASKED_OBSERVATION


def test_render_transcript_image_tool_return_is_placeholder():
    img = BinaryContent(data=b"\x89PNGbytes", media_type="image/png")
    history = [ModelRequest(parts=[
        ToolReturnPart(tool_name="read_file", content=img, tool_call_id="t1"),
    ])]
    out = render_transcript(history)
    # Unified with the shared binary-safe placeholder format (media type + KB),
    # the same one hooks/dispatch.py and stream_events.py now render.
    assert "[image image/png, 1 KB]" in out
    assert "PNGbytes" not in out


def test_render_transcript_list_content_with_binary_is_placeholder():
    # A list-content tool return (e.g. an MCP tool returning mixed text + image
    # blocks) must not fall through to _clip(str(list)), which would dump the
    # BinaryContent's repr (raw bytes) into the advisor-facing transcript.
    img = BinaryContent(data=b"\x89PNGbytes", media_type="image/png")
    history = [ModelRequest(parts=[
        ToolReturnPart(tool_name="mcp_tool", content=["caption", img], tool_call_id="t2"),
    ])]
    out = render_transcript(history)
    assert "[image image/png, 1 KB]" in out
    assert "caption" in out
    assert "PNGbytes" not in out


# --- revalidate: offload handles (spec 2026-07-26) ----------------------------


def _handle(path: str) -> str:
    """A realistic offload handle as _write_handle renders it."""
    return (
        "⚠️ Large bash result (30,000 chars, 200 lines) — full output "
        f"saved to `{path}`. Read more with read_file (it paginates) or "
        "grep that path.\n--- preview (first 40 lines) ---\nline one\nline two"
    )


def test_revalidate_annotates_dangling_handle_and_keeps_preview():
    h = _handle("/pad/bash-abc.txt")
    history = [_tool_return("t1", h)]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 1
    content = new_history[0].parts[0].content
    # Appended, not replaced: the preview survives.
    assert content == h + OFFLOAD_GONE_NOTE
    assert "line one" in content
    # Input never mutated.
    assert history[0].parts[0].content == h


def test_revalidate_leaves_live_handle_untouched():
    history = [_tool_return("t1", _handle("/pad/bash-live.txt"))]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: True)
    assert n == 0
    assert new_history is history


def test_revalidate_handle_note_is_idempotent():
    history = [_tool_return("t1", _handle("/pad/gone.txt"))]
    once, n1 = revalidate_elided_pointers(history, exists=lambda p: False)
    twice, n2 = revalidate_elided_pointers(once, exists=lambda p: False)
    assert n1 == 1 and n2 == 0
    assert twice is once


def test_revalidate_resolves_relative_handle_against_base(tmp_path):
    # Legacy histories can hold workspace-relative handles; base resolves them.
    live_rel = ".marim/output/live.txt"
    (tmp_path / ".marim" / "output").mkdir(parents=True)
    (tmp_path / live_rel).write_text("payload")
    history = [
        _tool_return("t1", _handle(live_rel)),
        _tool_return("t2", _handle(".marim/output/gone.txt")),
    ]
    new_history, n = revalidate_elided_pointers(history, base=tmp_path)
    assert n == 1
    assert new_history[0].parts[0].content == history[0].parts[0].content
    assert new_history[1].parts[0].content.endswith(OFFLOAD_GONE_NOTE)


def test_revalidate_mixed_pointer_and_handle_counts_both():
    pointer = _elided_pointer("/pad/e/gone.txt")
    h = _handle("/pad/bash-gone.txt")
    history = [_tool_return("t1", pointer), _tool_return("t2", h)]
    new_history, n = revalidate_elided_pointers(history, exists=lambda p: False)
    assert n == 2
    assert new_history[0].parts[0].content == MASKED_OBSERVATION  # replaced
    assert new_history[1].parts[0].content == h + OFFLOAD_GONE_NOTE  # annotated
