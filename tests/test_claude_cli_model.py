"""``ClaudeCliModel`` over the long-lived bidirectional ``claude`` process.

The pure helpers (history flattening, usage folding, chunk consumption) are
exercised directly; everything that drives a process runs against the scripted
fake ``claude`` in ``tests/fakes/fake_claude.py`` — a real subprocess speaking
the real stream-json control protocol, so the transport is never stubbed out.
"""

from __future__ import annotations

import asyncio
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    ModelResponse,
    PartDeltaEvent,
    SystemPromptPart,
    TextPart,
    ThinkingPartDelta,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters

from marim_harness.claude.env import CLI_BINARY_ENV, CLI_IDLE_TIMEOUT_ENV
from marim_harness.claude.process import ClaudeProcess
from marim_harness.claude.protocol import CLOSED
from marim_harness.config import claude_cli_model
from marim_harness.config.claude_cli_model import (
    SESSION_REF_PREFIX,
    ClaudeCliModel,
    DoneChunk,
    InitChunk,
    TextChunk,
    ThinkingChunk,
    ToolResultChunk,
    ToolUseChunk,
    backend_turn_note,
    consume_cli_stream,
    extract_system,
    flatten_history,
    format_activity_line,
    latest_user_text,
    request_usage_from_cli,
)
from marim_harness.config.context_report import ContextReport
from marim_harness.config.external_cli import CliModelError
from marim_harness.runtime.context import wrap_turn_context
from marim_harness.usage import COST_DETAIL_KEY
from tests.fakes import fake_claude_bin, read_claude_argv, read_claude_argvs, read_claude_log

# --- pure helpers -------------------------------------------------------------


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


def test_flatten_history_renders_tool_calls_and_returns():
    # A history produced by ANOTHER provider (e.g. openrouter using marim's tools)
    # carries ToolCallPart/ToolReturnPart. When claude-cli is switched in mid-session
    # and cold-starts, flatten_history must preserve that tool context, not drop it.
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="what's in config.py?")]),
        ModelResponse(
            parts=[
                TextPart(content="Let me read it."),
                ToolCallPart(tool_name="read_file", args={"path": "config.py"}, tool_call_id="t1"),
            ]
        ),
        ModelRequest(
            parts=[ToolReturnPart(tool_name="read_file", content="PORT = 8080", tool_call_id="t1")]
        ),
        ModelResponse(parts=[TextPart(content="It sets PORT to 8080.")]),
    ]
    out = flatten_history(msgs)
    # The assistant's tool call is rendered (name + args) so Claude sees what was done.
    assert "read_file" in out
    assert "config.py" in out
    # The tool's result is rendered so Claude sees what came back.
    assert "PORT = 8080" in out
    # Ordering preserved: the call appears before its result.
    assert out.index("config.py") < out.index("PORT = 8080")


def test_ephemeral_clone_is_stateless_read_only_with_cwd():
    main = ClaudeCliModel("opus")
    main.cwd = "/main"
    clone = main.ephemeral_clone(cwd="/ws")
    assert clone.ephemeral is True
    assert clone._model_id == "opus"
    assert clone.cwd == "/ws"
    assert clone.mode_getter() == "plan"  # aux agents run read-only


def test_fold_chunk_text_separates_segments():
    from marim_harness.config.claude_cli_model import (
        ToolUseChunk,
        fold_chunk_text,
    )

    first = fold_chunk_text(TextChunk("Surveying."), leading=True)
    tool = fold_chunk_text(ToolUseChunk("Bash", {"command": "ls | head -50"}, "t1"), leading=False)
    after = fold_chunk_text(TextChunk("Now sizes."), leading=False)
    text = first + tool + after
    # The old bug ran "head -50Now sizes." together; segments are blank-line separated.
    assert "head -50Now" not in text
    assert "▸ Bash ls | head -50" in text
    assert "\n\nNow sizes." in text


def test_fold_chunk_text_skips_results():
    from marim_harness.config.claude_cli_model import ToolResultChunk, fold_chunk_text

    assert fold_chunk_text(ToolResultChunk("t1", "ok", False), leading=False) == ""


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


def test_request_usage_cost_rounds_not_truncates():
    # Sub-micro-USD conversion must round (matching openrouter_cost) rather than
    # int()-truncate, or a fractional micro-USD is silently floored away.
    from marim_harness.usage import COST_DETAIL_KEY

    u = request_usage_from_cli({"input_tokens": 1, "output_tokens": 1}, total_cost_usd=0.0000007)
    # 0.0000007 * 1_000_000 == 0.7 -> round() == 1 (int() truncation would give 0).
    assert u.details[COST_DETAIL_KEY] == 1
    u2 = request_usage_from_cli({"input_tokens": 1, "output_tokens": 1}, total_cost_usd=0.123456789)
    assert u2.details[COST_DETAIL_KEY] == 123457  # 123456.789 rounded


def test_format_activity_line_summarizes_common_tools():
    assert format_activity_line("Read", {"file_path": "a/b.py"}) == "▸ Read a/b.py"
    assert format_activity_line("Bash", {"command": "ls -la"}) == "▸ Bash ls -la"
    assert format_activity_line("Grep", {"pattern": "foo"}) == "▸ Grep foo"
    # Unknown tool: name only, no crash.
    assert format_activity_line("TodoWrite", {"todos": []}) == "▸ TodoWrite"
    # The marker must NOT be an emoji-presentation glyph: terminals draw those
    # 2 cells wide while rich/Textual lay them out as 1, shifting the line and
    # clipping a character at the wrap ("Read" -> "Rea"). U+23FA (⏺) was such a glyph.
    assert "⏺" not in format_activity_line("Read", {"file_path": "a/b.py"})


def test_activity_line_names_agent_spawns():
    assert (
        format_activity_line("Agent", {"description": "Answer 2+2", "subagent_type": "Explore"})
        == "▸ Agent Answer 2+2"
    )


def test_cli_activity_events_builds_native_tool_events():
    from marim_harness.config.claude_cli_model import (
        ToolResultChunk,
        ToolUseChunk,
        cli_activity_events,
    )

    call = cli_activity_events(ToolUseChunk("Read", {"file_path": "a.py"}, "t1"))
    assert len(call) == 1
    assert isinstance(call[0], FunctionToolCallEvent)
    # Claude's "Read" is normalized to the harness name so it renders as a native card.
    assert call[0].part.tool_name == "read_file"
    assert call[0].part.tool_call_id == "t1"

    res = cli_activity_events(ToolResultChunk("t1", "boom", True))
    assert len(res) == 1
    assert isinstance(res[0], FunctionToolResultEvent)
    assert res[0].part.tool_call_id == "t1"
    assert res[0].part.outcome == "failed"


# --- consume_cli_stream (pure) ------------------------------------------------


def _delta(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {"type": "content_block_delta", "delta": {"type": "text_delta", "text": text}},
    }


def _think(text: str) -> dict:
    return {
        "type": "stream_event",
        "event": {
            "type": "content_block_delta",
            "delta": {"type": "thinking_delta", "thinking": text},
        },
    }


_INIT = {
    "type": "system",
    "subtype": "init",
    "session_id": "S1",
    "model": "claude-x",
    "claude_code_version": "2.1.261",
}


def _result(text: str, sid: str = "S1", **extra) -> dict:
    return {
        "type": "result",
        "subtype": "success",
        "result": text,
        "session_id": sid,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "total_cost_usd": 0.0,
        **extra,
    }


async def _collect(objs):
    async def gen():
        for o in objs:
            yield o

    out = []
    async for chunk in consume_cli_stream(gen()):
        out.append(chunk)
    return out


@pytest.mark.anyio
async def test_consume_streams_deltas_tools_and_done():
    chunks = await _collect(
        [
            _INIT,
            _think("hmm"),
            {
                "type": "assistant",
                "message": {"content": [{"type": "thinking", "thinking": "hmm"}]},
            },
            _delta("hel"),
            _delta("lo"),
            # The assistant object repeats the text; only its tool_use block counts.
            {
                "type": "assistant",
                "message": {
                    "content": [
                        {"type": "text", "text": "hello"},
                        {
                            "type": "tool_use",
                            "id": "t1",
                            "name": "Read",
                            "input": {"file_path": "/a"},
                        },
                    ]
                },
            },
            {
                "type": "user",
                "message": {
                    "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"}]
                },
            },
            {
                "type": "user",
                "isReplay": True,
                "message": {"content": [{"type": "text", "text": "hi"}]},
            },
            {"type": "system", "subtype": "status", "status": "compacting"},
            _result("hello"),
        ]
    )
    assert chunks[0] == InitChunk(session_id="S1", version="2.1.261", model="claude-x")
    assert chunks[1] == ThinkingChunk("hmm")
    assert chunks[2:4] == [TextChunk("hel"), TextChunk("lo")]
    assert chunks[4] == ToolUseChunk(name="Read", tool_input={"file_path": "/a"}, call_id="t1")
    assert chunks[5] == ToolResultChunk(call_id="t1", content="ok", is_error=False)
    done = chunks[-1]
    assert isinstance(done, DoneChunk) and done.complete and done.session_id == "S1"
    assert done.usage.input_tokens == 1 and done.aborted is False
    assert len(chunks) == 7  # the replay, the status and the assistant text block added nothing


@pytest.mark.anyio
async def test_consume_skips_subagent_child_traffic():
    chunks = await _collect(
        [
            {**_delta("child"), "parent_tool_use_id": "tsub"},
            {
                "type": "assistant",
                "parent_tool_use_id": "tsub",
                "message": {
                    "content": [{"type": "tool_use", "id": "c1", "name": "Read", "input": {}}]
                },
            },
            {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"},
            _delta("main"),
            _result("main"),
        ]
    )
    assert [type(c) for c in chunks] == [TextChunk, DoneChunk]
    assert chunks[0].delta == "main"


@pytest.mark.anyio
async def test_consume_aborted_result_is_complete_and_flagged():
    chunks = await _collect(
        [
            _delta("partial"),
            _result(
                "",
                subtype="error_during_execution",
                is_error=True,
                terminal_reason="aborted_streaming",
            ),
        ]
    )
    done = chunks[-1]
    assert done.complete is True and done.aborted is True and done.error_detail == ""


@pytest.mark.anyio
async def test_consume_errored_result_without_text_is_incomplete(caplog):
    with caplog.at_level("WARNING"):
        chunks = await _collect([_result("", subtype="error_max_turns", is_error=True)])
    done = chunks[-1]
    assert done.complete is False and "error_max_turns" in done.error_detail
    assert "error_max_turns" in caplog.text


@pytest.mark.anyio
async def test_consume_errored_result_with_text_keeps_partial(caplog):
    with caplog.at_level("WARNING"):
        chunks = await _collect(
            [_delta("half"), _result("", subtype="error_max_turns", is_error=True)]
        )
    done = chunks[-1]
    assert done.complete is True and "error_max_turns" in done.error_detail


@pytest.mark.anyio
async def test_consume_closed_is_incomplete_with_exit_detail():
    chunks = await _collect([_delta("a"), {"type": CLOSED, "stderr": "boom", "returncode": 3}])
    done = chunks[-1]
    assert done.complete is False and done.error_detail == "claude exited (code 3): boom"


@pytest.mark.anyio
async def test_consume_without_terminal_object_is_incomplete():
    (done,) = await _collect([])
    assert isinstance(done, DoneChunk) and done.complete is False


# --- the model against the fake claude ------------------------------------------


def _user(text: str, system: str = "SYS") -> list:
    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions=system)]


def _model(
    tmp_path: Path, monkeypatch, scenario: dict, model_id: str | None = "sonnet"
) -> ClaudeCliModel:
    monkeypatch.setenv(CLI_BINARY_ENV, fake_claude_bin(tmp_path, scenario))
    model = ClaudeCliModel(model_id)
    model.cwd = str(tmp_path)
    model.mode_getter = lambda: "auto"
    return model


async def _stream_text(model: ClaudeCliModel, messages: list | None = None) -> str:
    async with model.request_stream(
        messages or _user("hi"), None, ModelRequestParameters()
    ) as stream:
        async for _ in stream:
            pass
        final = stream.get()
    return "".join(getattr(p, "content", "") for p in final.parts)


def _user_texts(tmp_path: Path) -> list[str]:
    """Every user message the fake received, in order (steers included)."""
    out = []
    for msg in read_claude_log(tmp_path):
        if msg.get("type") == "user":
            out.append("".join(b.get("text", "") for b in msg["message"]["content"]))
    return out


@pytest.mark.anyio
async def test_request_returns_text_only_response_and_captures_session(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"session_id": "S9", "turns": [[{"text": "hello"}]]})
    refs: list[str] = []
    model.on_session_ref = refs.append
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert [type(p) for p in resp.parts] == [TextPart]
    # Prose arrives three characters at a time; the deltas concatenate, they are
    # not blank-line separated from one another.
    assert resp.parts[0].content == "hello"
    assert not any(isinstance(p, ToolCallPart) for p in resp.parts)
    assert model.session_id == "S9"
    assert refs == [SESSION_REF_PREFIX + "S9"]
    assert resp.usage.input_tokens == 7 and resp.usage.details[COST_DETAIL_KEY] == 1000
    assert resp.provider_name == "claude-cli"


@pytest.mark.anyio
async def test_first_turn_is_cold_with_system_and_isolation_flags(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    history = [
        ModelRequest(parts=[UserPromptPart(content="first")], instructions="SYS"),
        ModelResponse(parts=[TextPart(content="reply")]),
        ModelRequest(parts=[UserPromptPart(content="second")], instructions="SYS"),
    ]
    try:
        await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    argv = read_claude_argv(tmp_path)
    for flag in (
        "--safe-mode",
        "--strict-mcp-config",
        "--permission-prompt-tool",
        "--input-format",
    ):
        assert flag in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "SYS"
    assert argv[argv.index("--model") + 1] == "sonnet"
    assert "--resume" not in argv and "--no-session-persistence" not in argv
    assert _user_texts(tmp_path) == [flatten_history(history)]


@pytest.mark.parametrize("model_id", [None, ""])
@pytest.mark.anyio
async def test_blank_model_id_omits_model_flag(tmp_path, monkeypatch, model_id):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]}, model_id=model_id)
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "--model" not in read_claude_argv(tmp_path)
    assert model.model_name == "default"


@pytest.mark.anyio
async def test_second_turn_reuses_the_process_and_sends_only_new_text(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "one"}], [{"text": "two"}]]})
    history = _user("first")
    try:
        r1 = await model.request(history, None, ModelRequestParameters())
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="second")])]
        r2 = await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r2.parts[0].content == "two"
    assert len(read_claude_argvs(tmp_path)) == 1  # one launch
    assert _user_texts(tmp_path) == ["User: first", "second"]


@pytest.mark.anyio
async def test_dead_process_is_respawned_with_resume(tmp_path, monkeypatch):
    model = _model(
        tmp_path,
        monkeypatch,
        {"session_id": "S4", "known_sessions": ["S4"], "turns": [[{"text": "one"}]]},
    )
    try:
        await model.request(_user("a"), None, ModelRequestParameters())
        await model._process.aclose()  # idle reaper / crash stand-in
        r2 = await model.request(
            _user("a") + [ModelRequest(parts=[UserPromptPart(content="b")])],
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    # A respawned fake replays its script from the top, so the text repeats; what
    # this test pins is the *launch*: resumed by id, no system prompt re-sent, and
    # only the newest user text on the wire.
    assert r2.parts[0].content == "one"
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2
    assert argvs[1][argvs[1].index("--resume") + 1] == "S4"
    assert "--append-system-prompt" not in argvs[1]
    assert _user_texts(tmp_path) == ["User: a", "b"]


@pytest.mark.anyio
async def test_persisted_session_ref_is_resumed_on_first_turn(tmp_path, monkeypatch):
    model = _model(
        tmp_path, monkeypatch, {"known_sessions": ["OLD"], "turns": [[{"text": "back"}]]}
    )
    model.session_ref_getter = lambda: SESSION_REF_PREFIX + "OLD"
    try:
        resp = await model.request(_user("again"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "back"
    argv = read_claude_argv(tmp_path)
    assert argv[argv.index("--resume") + 1] == "OLD"
    assert _user_texts(tmp_path) == ["again"]  # newest text only, no flattening


@pytest.mark.anyio
async def test_foreign_session_ref_is_ignored(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    model.session_ref_getter = lambda: "codex-cli:THREAD"
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "--resume" not in read_claude_argv(tmp_path)


@pytest.mark.anyio
async def test_missing_session_falls_back_to_a_fresh_start(tmp_path, monkeypatch, caplog):
    model = _model(tmp_path, monkeypatch, {"known_sessions": [], "turns": [[{"text": "fresh"}]]})
    model.session_ref_getter = lambda: SESSION_REF_PREFIX + "GONE"
    refs: list[str] = []
    model.on_session_ref = refs.append
    try:
        with caplog.at_level("WARNING"):
            resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "fresh"
    argvs = read_claude_argvs(tmp_path)
    assert "--resume" in argvs[0] and "--resume" not in argvs[1]
    assert argvs[1][argvs[1].index("--append-system-prompt") + 1] == "SYS"
    assert refs == [SESSION_REF_PREFIX + "S1"]  # the new session replaces the stale ref
    assert "GONE" in caplog.text


@pytest.mark.anyio
async def test_ephemeral_model_never_resumes_never_persists_and_closes(tmp_path, monkeypatch):
    base = _model(tmp_path, monkeypatch, {"session_id": "LIVE", "turns": [[{"text": "t"}]]})
    base.session_ref_getter = lambda: SESSION_REF_PREFIX + "LIVE"
    refs: list[str] = []
    base.on_session_ref = refs.append
    clone = base.ephemeral_clone(cwd=str(tmp_path))
    assert clone.ephemeral and clone.mode_getter() == "plan" and clone.cwd == str(tmp_path)
    clone.session_ref_getter = base.session_ref_getter
    clone.on_session_ref = base.on_session_ref
    await clone.request(_user("title this"), None, ModelRequestParameters())
    await clone.request(_user("title this"), None, ModelRequestParameters())
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2  # a fresh process per call, closed after each
    for argv in argvs:
        assert "--resume" not in argv and "--no-session-persistence" in argv
    assert refs == [] and clone._process is None


@pytest.mark.anyio
async def test_failed_first_turn_on_an_ephemeral_clone_leaves_no_live_process(
    tmp_path, monkeypatch
):
    """`_start_turn` runs before `request`'s try/finally, so a failure there
    used to leave the clone's process running with nothing holding it — and the
    next aux call resumed the leak instead of starting clean."""
    base = _model(tmp_path, monkeypatch, {"turns": [[{"text": "t"}]]})
    clone = base.ephemeral_clone(cwd=str(tmp_path))
    spawned: list = []
    real_spawn = ClaudeCliModel._spawn

    async def spy(self, **kwargs):
        process = await real_spawn(self, **kwargs)
        spawned.append(process)
        return process

    monkeypatch.setattr(ClaudeCliModel, "_spawn", spy)

    # The real trigger is the silence timeout `next_turn_object` raises. The
    # fake replays the user message immediately, so it can never time out
    # naturally here — raise the same error from the same call instead.
    async def boom(process, handle):
        raise CliModelError("claude went silent")

    monkeypatch.setattr(claude_cli_model, "next_turn_object", boom)
    with pytest.raises(CliModelError):
        await clone.request(_user("title this"), None, ModelRequestParameters())
    assert len(spawned) == 1
    assert spawned[0].alive is False
    assert clone._process is None


@pytest.mark.anyio
async def test_parent_aclose_closes_a_clone_that_still_holds_a_process(tmp_path, monkeypatch):
    """Nothing else holds an aux clone: `Harness.aclose` knows only the
    session's current model, and the titler/summarizer/advisor keep theirs
    inside a pydantic-ai Agent. So the parent closes them."""
    base = _model(tmp_path, monkeypatch, {"turns": [[{"text": "t"}]]})
    clone = base.ephemeral_clone(cwd=str(tmp_path))
    # Stands in for any path that leaves a clone holding a live process.
    process = await clone._spawn(resume_id=None, system=None)
    assert process.alive is True
    await base.aclose()
    assert process.alive is False


@pytest.mark.anyio
async def test_idle_reaper_racing_a_turn_respawns_on_the_session_id(tmp_path, monkeypatch):
    """The reaper can be inside `aclose()` when the next turn starts. Reusing
    that process (it still had `alive is True`, and `_cancel_idle` only aborted
    the close half-done) is never right: the turn has to wait the close out and
    resume on a fresh process. The tell is the second launch — with `--resume`,
    so the conversation survives."""
    monkeypatch.setenv(CLI_IDLE_TIMEOUT_ENV, "0.2")
    scenario = {
        "session_id": "S4",
        "known_sessions": ["S4"],
        "turns": [[{"text": "one"}], [{"text": "two"}]],
    }
    model = _model(tmp_path, monkeypatch, scenario)
    entered = asyncio.Event()
    release = asyncio.Event()
    real_aclose = ClaudeProcess.aclose

    async def paused(self) -> None:
        entered.set()
        await release.wait()
        await real_aclose(self)

    try:
        await model.request(_user("a"), None, ModelRequestParameters())
        monkeypatch.setattr(ClaudeProcess, "aclose", paused)
        await asyncio.wait_for(entered.wait(), 5.0)  # the reaper is inside aclose()
        turn = asyncio.ensure_future(model.request(_user("b"), None, ModelRequestParameters()))
        await asyncio.sleep(0.05)  # let the turn reach _ensure_process
        assert model._process is not None and model._process.closing
        release.set()
        second = await asyncio.wait_for(turn, 20.0)
    finally:
        release.set()
        await model.aclose()
    assert second.parts[0].content == "one"  # the respawned fake replays its first turn
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2
    assert "--resume" not in argvs[0]
    assert argvs[1][argvs[1].index("--resume") + 1] == "S4"


@pytest.mark.anyio
async def test_request_runs_claude_in_the_configured_cwd(tmp_path, monkeypatch):
    work = tmp_path / "work"
    work.mkdir()
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    model.cwd = str(work)
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
        assert model._process.init_info["cwd"] == str(work.resolve())
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_response_timestamp_is_per_request(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}]]})
    try:
        r1 = await model.request(_user("hi"), None, ModelRequestParameters())
        r2 = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r2.timestamp > r1.timestamp


@pytest.mark.anyio
async def test_request_raises_with_exit_detail_when_claude_dies(tmp_path, monkeypatch):
    scenario = {"turns": [[{"text": "a"}, {"exit": {"code": 3, "stderr": "not logged in"}}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    try:
        with pytest.raises(CliModelError) as exc:
            await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert "no result" in str(exc.value)
    assert "claude exited (code 3): not logged in" in str(exc.value)


@pytest.mark.anyio
async def test_request_stream_raises_with_exit_detail_when_claude_dies(tmp_path, monkeypatch):
    model = _model(
        tmp_path, monkeypatch, {"turns": [[{"exit": {"code": 2, "stderr": "bad flag"}}]]}
    )
    try:
        with pytest.raises(CliModelError) as exc:
            await _stream_text(model)
    finally:
        await model.aclose()
    assert "claude exited (code 2): bad flag" in str(exc.value)


@pytest.mark.anyio
async def test_missing_binary_raises_install_hint(tmp_path, monkeypatch):
    monkeypatch.setenv(CLI_BINARY_ENV, "definitely-not-a-claude-xyz")
    model = ClaudeCliModel("sonnet")
    with pytest.raises(CliModelError) as exc:
        await model.request(_user("hi"), None, ModelRequestParameters())
    assert "claude CLI not found" in str(exc.value)


@pytest.mark.anyio
async def test_request_stream_yields_text_and_thinking_events(tmp_path, monkeypatch):
    scenario = {"turns": [[{"thinking": "let me see"}, {"text": "Hello!"}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    events = []
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for ev in stream:
                events.append(ev)
            final = stream.get()
    finally:
        await model.aclose()
    thinking = "".join(
        ev.delta.content_delta
        for ev in events
        if isinstance(ev, PartDeltaEvent) and isinstance(ev.delta, ThinkingPartDelta)
    )
    assert thinking == "let me see"
    assert "".join(getattr(p, "content", "") for p in final.parts if isinstance(p, TextPart)) == (
        "Hello!"
    )
    assert final.usage.input_tokens == 7


@pytest.mark.anyio
async def test_request_stream_pushes_tool_cards_and_keeps_response_text_only(tmp_path, monkeypatch):
    scenario = {
        "turns": [
            [
                {"text": "Looking."},
                {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}},
                {"tool_result": {"id": "t1", "content": "print(1)"}},
                {"text": "Done."},
            ]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    activity: list = []

    async def on_activity(events):
        activity.extend(events)

    model.on_activity = on_activity
    try:
        text = await _stream_text(model)
    finally:
        await model.aclose()
    assert text == "Looking.Done."
    calls = [e for e in activity if isinstance(e, FunctionToolCallEvent)]
    results = [e for e in activity if isinstance(e, FunctionToolResultEvent)]
    assert calls[0].part.tool_name == "read_file" and calls[0].part.args == {"path": "/a.py"}
    assert isinstance(results[0].part, ToolReturnPart) and results[0].part.content == "print(1)"


@pytest.mark.anyio
async def test_headless_stream_folds_tool_activity_into_text(tmp_path, monkeypatch):
    scenario = {
        "turns": [
            [{"tool_use": {"id": "t1", "name": "Bash", "input": {"command": "ls"}}}, {"text": "ok"}]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    try:
        text = await _stream_text(model)
    finally:
        await model.aclose()
    assert text == "▸ Bash ls\n\nok"


@pytest.mark.anyio
async def test_request_stream_routes_claude_subagents_to_side_channels(tmp_path, monkeypatch):
    scenario = {
        "turns": [
            [
                {
                    "raw": {
                        "type": "assistant",
                        "message": {
                            "id": "m1",
                            "content": [
                                {
                                    "type": "tool_use",
                                    "id": "tsub",
                                    "name": "Agent",
                                    "input": {
                                        "description": "d",
                                        "subagent_type": "Explore",
                                        "prompt": "p",
                                    },
                                }
                            ],
                        },
                    }
                },
                {"raw": {"type": "system", "subtype": "task_started", "tool_use_id": "tsub"}},
                {
                    "raw": {
                        "type": "user",
                        "message": {
                            "content": [
                                {
                                    "type": "tool_result",
                                    "tool_use_id": "tsub",
                                    "content": "Async agent launched...",
                                }
                            ]
                        },
                    }
                },
                {
                    "raw": {
                        "type": "stream_event",
                        "parent_tool_use_id": "tsub",
                        "event": {
                            "type": "content_block_delta",
                            "delta": {"type": "text_delta", "text": "4"},
                        },
                    }
                },
                {
                    "raw": {
                        "type": "assistant",
                        "parent_tool_use_id": "tsub",
                        "message": {
                            "id": "m2",
                            "model": "claude-haiku-4-5",
                            "usage": {"input_tokens": 3, "output_tokens": 2},
                            "content": [{"type": "text", "text": "4"}],
                        },
                    }
                },
                {
                    "raw": {
                        "type": "system",
                        "subtype": "task_notification",
                        "tool_use_id": "tsub",
                        "status": "completed",
                        "summary": "4",
                    }
                },
                {"text": "Four."},
            ]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    activity: list = []
    sub_events: list = []
    sub_models: list = []

    async def on_activity(events):
        activity.extend(events)

    async def on_subagent(sid, event, usage):
        sub_events.append((sid, event, usage))

    async def on_subagent_model(sid, m):
        sub_models.append((sid, m))

    model.on_activity = on_activity
    model.on_subagent = on_subagent
    model.on_subagent_model = on_subagent_model
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass
            resp = stream.get()
    finally:
        await model.aclose()
    text = "".join(getattr(p, "content", "") for p in resp.parts)
    spawn_names = [e.part.tool_name for e in activity if hasattr(e, "part")]
    assert spawn_names.count("spawn_agent") == 2
    assert sub_events and all(sid == "tsub" for sid, _, _ in sub_events)
    assert any(u is not None and u.output_tokens == 2 for _, _, u in sub_events)
    assert ("tsub", "claude-haiku-4-5") in sub_models
    assert text == "Four."  # the child's "4" (delta AND block) never entered the main text
    # The synthesized spawn_agent call/return bypass TextFolder but still
    # reach the ledger (through the same recording wrap), so a resumed
    # transcript rebuilds the spawn card; the prose after the spawn starts a
    # fresh part below it, exactly like an ordinary tool card.
    from marim_harness.config.external_cli import CLI_ACTIVITY_KEY

    assert resp.provider_details is not None
    ledger = resp.provider_details[CLI_ACTIVITY_KEY]
    assert [e["kind"] for e in ledger] == ["call", "result", "part"]
    assert ledger[0]["name"] == "spawn_agent" and ledger[0]["id"] == "tsub"
    assert ledger[1]["id"] == "tsub" and ledger[1]["outcome"] == "success"
    assert ledger[2] == {"kind": "part", "index": 0}


@pytest.mark.anyio
async def test_stream_abandon_interrupts_the_turn_and_keeps_the_process(tmp_path, monkeypatch):
    scenario = {"turns": [[{"text": "a"}, {"await_interrupt": True}], [{"text": "next"}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                break  # abandon mid-turn
        assert model._process.alive and not model._process.turn_open
        # the process is still usable: the next turn runs on it
        resp = await model.request(
            _user("hi") + [ModelRequest(parts=[UserPromptPart(content="more")])],
            None,
            ModelRequestParameters(),
        )
    finally:
        await model.aclose()
    assert resp.parts[0].content == "next"
    assert len(read_claude_argvs(tmp_path)) == 1
    assert any(
        m.get("type") == "control_request" and m["request"]["subtype"] == "interrupt"
        for m in read_claude_log(tmp_path)
    )


@pytest.mark.anyio
async def test_request_cancel_interrupts_the_turn(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"await_interrupt": True}]]})
    task = asyncio.ensure_future(model.request(_user("hi"), None, ModelRequestParameters()))
    await asyncio.sleep(0.5)
    task.cancel()
    try:
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert not model._process.turn_open
    finally:
        await model.aclose()


@pytest.mark.anyio
async def test_steer_folds_into_the_open_turn(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "a"}, {"await_user": True}]]})
    assert model.steer("early") is False  # no turn open yet
    texts: list[str] = []
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for ev in stream:
                if isinstance(ev, PartDeltaEvent) and not isinstance(ev.delta, ThinkingPartDelta):
                    texts.append(ev.delta.content_delta)
                if "".join(texts) == "a" and len(texts) == 1:
                    assert model.steer("go left") is True
    finally:
        await model.aclose()
    assert "".join(texts) == "aheard: go left"


@pytest.mark.anyio
async def test_approval_prompts_go_through_the_broker(tmp_path, monkeypatch):
    outside = tmp_path.parent / "elsewhere.txt"
    step = {
        "can_use_tool": {"tool_name": "Write", "input": {"file_path": str(outside), "content": "x"}}
    }
    model = _model(tmp_path, monkeypatch, {"turns": [[step]]})
    seen: list = []

    async def approve(call):
        seen.append(call)
        return True

    model.request_approval = approve
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    # Headless folds the tool line in above the prose (see fold_chunk_text).
    assert resp.parts[0].content.endswith("Write done")
    assert seen[0].tool_name == "write_file" and seen[0].args["reason"].startswith(
        "outside workspace"
    )


@pytest.mark.anyio
async def test_plan_mode_denies_writes_with_the_wire_message(tmp_path, monkeypatch):
    step = {
        "can_use_tool": {
            "tool_name": "Write",
            "input": {"file_path": str(tmp_path / "a"), "content": "x"},
        }
    }
    model = _model(tmp_path, monkeypatch, {"turns": [[step]]})
    model.mode_getter = lambda: "plan"
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content.endswith(
        "denied: plan mode: read-only — describe the change instead of making it"
    )
    log = read_claude_log(tmp_path)
    denials = [m for m in log if m.get("type") == "control_response"]
    assert denials and denials[0]["response"]["response"]["behavior"] == "deny"


@pytest.mark.anyio
async def test_plan_mode_denies_webfetch_end_to_end(tmp_path, monkeypatch):
    """Plan mode on the main loop must refuse Claude's own network tools: they
    are non-mutating, so only the classifier's network flag stops the egress."""
    step = {
        "can_use_tool": {
            "tool_name": "WebFetch",
            "input": {"url": "https://example.invalid/x"},
            "tool_use_id": "w1",
        }
    }
    model = _model(tmp_path, monkeypatch, {"turns": [[step]]})
    model.mode_getter = lambda: "plan"
    try:
        resp = await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content.endswith(
        "denied: plan mode: read-only — describe the change instead of making it"
    )


@pytest.mark.anyio
async def test_old_claude_version_warns_once(tmp_path, monkeypatch, caplog):
    import marim_harness.config.claude_cli_model as mod

    monkeypatch.setattr(mod, "_version_warned", False)
    model = _model(tmp_path, monkeypatch, {"version": "1.0.99", "turns": [[{"text": "a"}]]})
    try:
        with caplog.at_level("WARNING"):
            await model.request(_user("hi"), None, ModelRequestParameters())
            await model.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert caplog.text.count("1.0.99") == 1


@pytest.mark.anyio
async def test_request_stream_records_tool_activity_ledger_for_persistence(tmp_path, monkeypatch):
    """Cards mode: the tool calls pushed out-of-band are ALSO recorded on the
    response's provider_details (interleaved with the part indexes) so the
    controller can persist them as real tool messages; fold mode (no UI)
    records nothing — its ▸ lines are the record."""
    from marim_harness.config.external_cli import CLI_ACTIVITY_KEY

    scenario = {
        "turns": [
            [
                {"text": "Looking."},
                {"tool_use": {"id": "t1", "name": "Read", "input": {"file_path": "/a.py"}}},
                {"tool_result": {"id": "t1", "content": "print(1)"}},
                {"text": "Done."},
            ]
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)

    async def on_activity(events):
        pass

    model.on_activity = on_activity
    try:
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass
            resp = stream.get()
    finally:
        await model.aclose()
    assert [p.content for p in resp.parts] == ["Looking.", "Done."]
    assert resp.provider_details is not None
    assert resp.provider_details[CLI_ACTIVITY_KEY] == [
        {"kind": "part", "index": 0},
        {"kind": "call", "id": "t1", "name": "read_file", "args": {"path": "/a.py"}},
        {"kind": "result", "id": "t1", "content": "print(1)", "outcome": "success"},
        {"kind": "part", "index": 1},
    ]

    fold = _model(tmp_path, monkeypatch, scenario)
    try:
        async with fold.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass
            folded = stream.get()
    finally:
        await fold.aclose()
    assert folded.provider_details is None and "▸" in folded.parts[0].content


# --- context report, per-turn cost, quota (Phase 1 of the CLI parity roadmap) --------


def test_cost_meter_bills_the_delta_since_the_previous_result():
    from marim_harness.config.claude_cli_model import CostMeter

    meter = CostMeter()
    # The live 2.1.270 numbers: cumulative 0.0109 → 0.0137 → 0.0162. Each
    # charge is a difference of micro-USD-rounded totals (so 0.0108757
    # bills as 0.010876), never the raw float difference.
    assert meter.charge(0.0108757) == pytest.approx(0.010876)
    assert meter.charge(0.0136693) == pytest.approx(0.002793)
    assert meter.charge(0.0161742) == pytest.approx(0.002505)
    assert meter.charge(None) is None
    # A total that went DOWN (the CLI documents that a mid-session /clear
    # resets it) is never a negative charge: the new total is what was spent
    # since the reset, so it is billed whole and becomes the baseline.
    assert meter.charge(0.001) == pytest.approx(0.001)
    assert meter.charge(0.002) == pytest.approx(0.001)
    # `behind` tells a result produced BEFORE the one last billed (a buffered
    # CLI turn served late) from a reset: the caller skips the charge.
    assert meter.behind(0.0015) is True
    assert meter.behind(0.002) is False and meter.behind(0.003) is False
    assert meter.behind(None) is False
    meter.reset()
    assert meter.charge(0.0005) == pytest.approx(0.0005)


def test_cost_meter_deltas_sum_exactly_to_the_cli_total_in_micro_usd():
    """The ledger stores each turn's charge as rounded micro-USD; the meter
    bills differences of rounded TOTALS so those integers telescope to the
    CLI's own total instead of drifting a micro-dollar per turn."""
    from marim_harness.config.claude_cli_model import CostMeter

    meter = CostMeter()
    totals = [0.0108757, 0.0136693, 0.0161742]
    billed = [round(meter.charge(t) * 1_000_000) for t in totals]  # type: ignore[operator]
    assert billed == [10_876, 2_793, 2_505]
    assert sum(billed) == round(totals[-1] * 1_000_000) == 16_174


def test_charge_cost_sets_only_the_cost_detail():
    from pydantic_ai.usage import RequestUsage

    from marim_harness.config.claude_cli_model import charge_cost

    base = RequestUsage(input_tokens=5, output_tokens=2, cache_read_tokens=3, details={"x": 1})
    assert charge_cost(base, None) is base
    charged = charge_cost(base, 0.0000007)
    assert charged.input_tokens == 5 and charged.cache_read_tokens == 3
    assert charged.details == {"x": 1, COST_DETAIL_KEY: 1}


@pytest.mark.anyio
async def test_consume_reports_prompt_usage_and_result_facts():
    """An ``assistant`` object's usage is the request's size (a
    ``PromptUsageChunk`` before its tool_use blocks); the ``result`` carries
    the running cost and the main model's window on the DoneChunk, and its
    usage carries NO cost — the model bills the per-turn delta."""
    from marim_harness.config.claude_cli_model import PromptUsageChunk

    chunks = await _collect(
        [
            _INIT,
            {
                "type": "assistant",
                "message": {
                    "usage": {
                        "input_tokens": 3,
                        "cache_read_input_tokens": 27_000,
                        "cache_creation_input_tokens": 500,
                        "output_tokens": 9,
                    },
                    "content": [{"type": "tool_use", "id": "t1", "name": "Read", "input": {}}],
                },
            },
            _result(
                "done",
                total_cost_usd=0.0136693,
                modelUsage={
                    "claude-haiku-4-5-20251001": {"inputTokens": 50, "contextWindow": 200_000},
                    "claude-opus-4-1": {
                        "inputTokens": 3,
                        "cacheReadInputTokens": 27_000,
                        "contextWindow": 1_000_000,
                    },
                    "junk": {"contextWindow": "wide"},
                },
            ),
        ]
    )
    assert chunks[1] == PromptUsageChunk(27_503)  # no message.model on this one
    assert isinstance(chunks[2], ToolUseChunk)
    done = chunks[-1]
    assert isinstance(done, DoneChunk) and done.complete
    assert done.cumulative_cost_usd == pytest.approx(0.0136693)
    assert done.context_window == 1_000_000  # the model that did the most input work
    assert done.context_windows == {
        "claude-haiku-4-5-20251001": 200_000,
        "claude-opus-4-1": 1_000_000,
    }
    assert COST_DETAIL_KEY not in done.usage.details


@pytest.mark.anyio
async def test_consume_without_model_usage_leaves_the_window_unknown():
    chunks = await _collect([_INIT, _result("ok", total_cost_usd=None, modelUsage=None)])
    done = chunks[-1]
    assert isinstance(done, DoneChunk)
    assert done.context_window is None and done.cumulative_cost_usd is None


def test_context_windows_ignore_a_malformed_model_usage():
    from marim_harness.config.claude_cli_model import _context_window, _context_windows

    for junk in (["not", "a", "mapping"], "string", 7, None):
        assert _context_windows({"modelUsage": junk}) == {}
        assert _context_window({"modelUsage": junk}) is None
    assert _context_windows({"modelUsage": {"m": {"contextWindow": "big"}, "n": 3}}) == {}


_USAGE_REPORT = {
    "subscription_type": "max",
    "rate_limits_available": True,
    "rate_limits": {
        "five_hour": {"utilization": 11, "resets_at": "2026-09-13T22:40:00+00:00"},
        "seven_day": {"utilization": 59, "resets_at": "2026-09-16T18:00:00+00:00"},
    },
}


def _cost_scenario() -> dict:
    """Three turns whose results carry a CUMULATIVE cost (0.001 / 0.003 /
    0.006) and a window; each turn's assistant object reports a growing
    prompt size. The fake answers ``get_usage`` with a Max-plan reading."""

    def turn(text: str, prompt: int, cumulative: float) -> list:
        return [
            {
                "raw": {
                    "type": "assistant",
                    "message": {
                        "usage": {"input_tokens": prompt, "output_tokens": 1},
                        "content": [{"type": "text", "text": text}],
                    },
                    "session_id": "S1",
                }
            },
            {"text": text},
            {
                "result": {
                    "total_cost_usd": cumulative,
                    "modelUsage": {
                        "claude-sonnet-4-6": {"inputTokens": 1, "contextWindow": 200_000}
                    },
                }
            },
        ]

    return {
        "usage_report": _USAGE_REPORT,
        "turns": [
            turn("one", 30_000, 0.001),
            turn("two", 31_000, 0.003),
            turn("three", 33_000, 0.006),
        ],
    }


@pytest.mark.anyio
async def test_request_bills_each_turn_its_own_cost_and_keeps_the_context_report(
    tmp_path, monkeypatch
):
    from marim_harness.config.context_report import CONTEXT_REPORT_KEY, ContextReport
    from marim_harness.config.quota import QuotaHint, QuotaWindow

    model = _model(tmp_path, monkeypatch, _cost_scenario())
    assert model.context_report is None and model.quota_hint is None
    history = _user("first")
    try:
        r1 = await model.request(history, None, ModelRequestParameters())
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="second")])]
        r2 = await model.request(history, None, ModelRequestParameters())
        history = history + [r2, ModelRequest(parts=[UserPromptPart(content="third")])]
        r3 = await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    # Cumulative on the wire, per-turn in the ledger: 0.001, 0.002, 0.003.
    assert [r.usage.details[COST_DETAIL_KEY] for r in (r1, r2, r3)] == [1000, 2000, 3000]
    assert model.context_report == ContextReport(33_000, 200_000)
    # Each response persists the report it ended with (the window is known
    # from the first result on).
    assert r1.provider_details == {CONTEXT_REPORT_KEY: {"used": 30_000, "window": 200_000}}
    assert r3.provider_details == {CONTEXT_REPORT_KEY: {"used": 33_000, "window": 200_000}}
    assert model.quota_hint == QuotaHint(QuotaWindow(11, 300), QuotaWindow(59, 10080))
    # The poll went out once per turn, after each result, with skip_behaviors.
    polls = [
        m
        for m in read_claude_log(tmp_path)
        if (m.get("request") or {}).get("subtype") == "get_usage"
    ]
    assert len(polls) == 3 and all(m["request"]["skip_behaviors"] is True for m in polls)


@pytest.mark.anyio
async def test_request_stream_settles_cost_report_and_quota_like_request(tmp_path, monkeypatch):
    from marim_harness.config.context_report import CONTEXT_REPORT_KEY, ContextReport

    model = _model(tmp_path, monkeypatch, _cost_scenario())
    seen: list = []
    history = _user("first")
    try:
        for _ in range(2):
            async with model.request_stream(history, None, ModelRequestParameters()) as stream:
                async for _ in stream:
                    if model.context_report is not None:
                        seen.append(model.context_report)
                resp = stream.get()
            history = history + [resp, ModelRequest(parts=[UserPromptPart(content="again")])]
    finally:
        await model.aclose()
    assert resp.usage.details[COST_DETAIL_KEY] == 2000  # 0.003 − 0.001
    assert resp.provider_details == {CONTEXT_REPORT_KEY: {"used": 31_000, "window": 200_000}}
    assert model.context_report == ContextReport(31_000, 200_000)
    # The report moved while the FIRST turn streamed (before its result knew
    # the window), so the status bar sees it mid-turn.
    assert ContextReport(30_000, None) in seen
    assert model.quota_hint is not None and model.quota_hint.render() == "quota 11% (5h) · 59% (1w)"


@pytest.mark.anyio
async def test_respawn_resets_the_cost_baseline(tmp_path, monkeypatch):
    """A fresh process restarts the CLI's running total at zero: its first
    result must be billed in full, not against the dead process's total
    (which would bill it as a zero-cost turn). The fake replays turn 1 for
    the new process, so both results carry the same cumulative 0.005."""
    scenario = {"turns": [[{"result": {"total_cost_usd": 0.005}}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    history = _user("first")
    try:
        r1 = await model.request(history, None, ModelRequestParameters())
        assert model._process is not None
        await model._process.aclose()  # the idle reaper / a crash
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="second")])]
        r2 = await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert r1.usage.details[COST_DETAIL_KEY] == 5000
    assert r2.usage.details[COST_DETAIL_KEY] == 5000
    assert len(read_claude_argvs(tmp_path)) > 1  # a new process served turn two


@pytest.mark.anyio
async def test_context_window_follows_the_model_behind_the_last_request(tmp_path, monkeypatch):
    """After a fallback from a 1M-window model to a 200k one, the old model's
    cumulative input keeps it the heaviest ``modelUsage`` entry — the window
    must come from the model the last ``assistant`` event named, and the
    carried window is dropped the moment a request names a new model."""
    from marim_harness.config.context_report import ContextReport

    usage = {
        "claude-opus-4-1": {"inputTokens": 900_000, "contextWindow": 1_000_000},
        "claude-sonnet-4-6": {"inputTokens": 50_000, "contextWindow": 200_000},
    }

    def turn(model: str, prompt: int) -> list:
        return [
            {
                "raw": {
                    "type": "assistant",
                    "message": {
                        "model": model,
                        "usage": {"input_tokens": prompt, "output_tokens": 1},
                        "content": [{"type": "text", "text": "x"}],
                    },
                    "session_id": "S1",
                }
            },
            {"text": "x"},
            {"result": {"modelUsage": usage}},
        ]

    scenario = {"turns": [turn("claude-opus-4-1", 800_000), turn("claude-sonnet-4-6", 40_000)]}
    model = _model(tmp_path, monkeypatch, scenario)
    seen: list = []
    history = _user("first")
    try:
        r1 = await model.request(history, None, ModelRequestParameters())
        assert model.context_report == ContextReport(800_000, 1_000_000)
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="second")])]
        async with model.request_stream(history, None, ModelRequestParameters()) as stream:
            async for _ in stream:
                seen.append(model.context_report)
    finally:
        await model.aclose()
    # Mid-turn the reading named the new model's size with NO window (the
    # 1M one no longer applies); the result then filled in sonnet's.
    assert ContextReport(40_000, None) in seen
    assert model.context_report == ContextReport(40_000, 200_000)


@pytest.mark.anyio
async def test_a_failed_turn_leaves_its_spend_for_the_next_turn_to_bill(tmp_path, monkeypatch):
    """A ``result`` that errored with no text raises, and the raise records no
    usage — so the meter is NOT advanced past it: the next successful turn
    bills the failed turn's spend too, and the ledger's total still equals
    the CLI's running total (0.006) instead of losing the failed 0.004."""
    scenario = {
        "turns": [
            [
                {
                    "result": {
                        "subtype": "error_during_execution",
                        "is_error": True,
                        "result": "",
                        "total_cost_usd": 0.004,
                    }
                }
            ],
            [{"text": "ok"}, {"result": {"total_cost_usd": 0.006}}],
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    history = _user("first")
    try:
        with pytest.raises(CliModelError):
            await model.request(history, None, ModelRequestParameters())
        resp = await model.request(history, None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.usage.details[COST_DETAIL_KEY] == 6000
    assert len(read_claude_argvs(tmp_path)) == 1  # same process, same running total


@pytest.mark.anyio
async def test_quota_poll_failure_is_ignored_and_ephemeral_clones_skip_it(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"usage_report": {"rate_limits_available": False}})
    clone = model.ephemeral_clone(cwd=str(tmp_path))
    try:
        await model.request(_user("hi"), None, ModelRequestParameters())
        await clone.request(_user("hi"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert model.quota_hint is None and clone.quota_hint is None
    polls = [
        m
        for m in read_claude_log(tmp_path)
        if (m.get("request") or {}).get("subtype") == "get_usage"
    ]
    assert len(polls) == 1  # the main model only; the clone never asks


@pytest.mark.anyio
async def test_a_failed_quota_poll_clears_the_previous_hint(tmp_path, monkeypatch):
    """The status bar renders any hint the adapter holds, so a poll that
    fails must drop the previous reading instead of leaving it up stale."""
    from types import SimpleNamespace

    from marim_harness.config.quota import QuotaHint, QuotaWindow

    model = _model(tmp_path, monkeypatch, {})
    model.quota_hint = QuotaHint(QuotaWindow(11, 300), None)

    async def failing_read_usage():
        raise TimeoutError("no answer")

    await model._refresh_quota(SimpleNamespace(alive=True, read_usage=failing_read_usage))  # type: ignore[arg-type]
    assert model.quota_hint is None


# --- background sub-agents: the turn Claude runs on its own ---------------------

_NOTIFICATION = {
    "type": "system",
    "subtype": "task_notification",
    "tool_use_id": "tu1",
    "status": "completed",
    "summary": "pong",
}
_OWN_TURN = {"prelude": [_NOTIFICATION], "steps": [{"text": "Agent completed: pong"}]}
_SPAWN_TURN = [
    {"tool_use": {"id": "tu1", "name": "Agent", "input": {"description": "Explore"}}},
    {
        "raw": {
            "type": "system",
            "subtype": "task_started",
            "tool_use_id": "tu1",
            "is_backgrounded": True,
        }
    },
    {"tool_result": {"id": "tu1", "content": "Async agent launched successfully"}},
    {"text": "Launched."},
    {"after_turn": _OWN_TURN},
]


def test_backend_turn_note_lists_the_reports():
    note = backend_turn_note([_NOTIFICATION, {"type": "system", "subtype": "init"}])
    assert note.startswith("[background sub-agent reports")
    assert "sub-agent tu1 completed: pong" in note and note.endswith("]")
    bare = backend_turn_note([{"type": "system", "subtype": "init"}])
    assert "ran a turn of its own" in bare


def _autonomous(note: str) -> list:
    """The prompt marim's autonomous turn carries: context only, nothing typed."""
    prompt = wrap_turn_context(note, "")
    return _user("hi") + [ModelRequest(parts=[UserPromptPart(content=prompt)])]


async def _wait_for(predicate, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        assert asyncio.get_running_loop().time() < deadline, "condition never held"
        await asyncio.sleep(0.02)


@pytest.mark.anyio
async def test_autonomous_turn_shows_the_cli_own_turn_and_settles_the_card(tmp_path, monkeypatch):
    """The spawn card opened in turn 1 settles in the autonomous turn that
    shows Claude's reaction, and nothing is sent to the CLI for it: its
    history already holds that turn."""
    model = _model(tmp_path, monkeypatch, {"turns": [_SPAWN_TURN]})
    notes: list[str] = []
    activity: list = []

    async def on_activity(events):
        activity.extend(events)

    async def on_subagent(sid, event, usage):
        pass  # the demux runs only with a sub-agent sink bound

    model.on_backend_turn = notes.append
    model.on_activity = on_activity
    model.on_subagent = on_subagent
    try:
        first = await _stream_text(model)
        await _wait_for(lambda: bool(notes))
        assert model._process is not None and model._process.has_unsolicited
        second = await _stream_text(model, _autonomous(notes[0]))
    finally:
        await model.aclose()
    assert first == "Launched." and second == "Agent completed: pong"
    assert "sub-agent tu1 completed: pong" in notes[0]
    assert _user_texts(tmp_path) == ["User: hi"]  # the autonomous turn sent nothing
    calls = [e.part.tool_call_id for e in activity if isinstance(e, FunctionToolCallEvent)]
    results = [
        (e.part.tool_call_id, e.part.content)
        for e in activity
        if isinstance(e, FunctionToolResultEvent)
    ]
    assert calls == ["tu1"] and results == [("tu1", "pong")]


@pytest.mark.anyio
async def test_typed_turn_leaves_the_cli_own_turn_buffered(tmp_path, monkeypatch):
    scenario = {"turns": [_SPAWN_TURN, [{"text": "two"}]]}
    model = _model(tmp_path, monkeypatch, scenario)
    notes: list[str] = []
    model.on_backend_turn = notes.append
    try:
        await _stream_text(model)
        await _wait_for(lambda: bool(notes))
        typed = await _stream_text(
            model, _user("hi") + [ModelRequest(parts=[UserPromptPart(content="more")])]
        )
        assert model._process is not None and model._process.has_unsolicited
        shown = await _stream_text(model, _autonomous(notes[0]))
    finally:
        await model.aclose()
    assert typed == "two" and shown == "Agent completed: pong"
    assert _user_texts(tmp_path) == ["User: hi", "more"]


@pytest.mark.anyio
async def test_cli_own_turn_served_after_a_typed_turn_is_not_billed_twice(tmp_path, monkeypatch):
    """The CLI's running total is cumulative, so the typed turn that went out
    while the own turn sat buffered already paid for it (0.005 → 0.012 covers
    the own turn's 0.008). Served late, the own turn's older total must not
    read as a /clear reset billed whole, nor drag the baseline back so the
    next turn pays for everything a second time."""
    own_turn = {
        "prelude": [_NOTIFICATION],
        "steps": [{"text": "Agent completed: pong"}, {"result": {"total_cost_usd": 0.008}}],
    }
    spawn_turn = [
        *_SPAWN_TURN[:-1],
        {"result": {"total_cost_usd": 0.005}},
        {"after_turn": own_turn},
    ]
    scenario = {
        "turns": [
            spawn_turn,
            [{"text": "two"}, {"result": {"total_cost_usd": 0.012}}],
            [{"text": "three"}, {"result": {"total_cost_usd": 0.015}}],
        ]
    }
    model = _model(tmp_path, monkeypatch, scenario)
    notes: list[str] = []
    model.on_backend_turn = notes.append
    params = ModelRequestParameters()
    history = _user("hi")
    try:
        r1 = await model.request(history, None, params)
        await _wait_for(lambda: bool(notes))
        history = history + [r1, ModelRequest(parts=[UserPromptPart(content="more")])]
        r2 = await model.request(history, None, params)
        history = history + [
            r2,
            ModelRequest(parts=[UserPromptPart(content=wrap_turn_context(notes[0], ""))]),
        ]
        r3 = await model.request(history, None, params)
        history = history + [r3, ModelRequest(parts=[UserPromptPart(content="again")])]
        r4 = await model.request(history, None, params)
    finally:
        await model.aclose()
    assert [r.parts[0].content for r in (r2, r3, r4)] == [  # type: ignore[union-attr]
        "two",
        "Agent completed: pong",
        "three",
    ]
    billed = [r.usage.details[COST_DETAIL_KEY] for r in (r1, r2, r3, r4)]
    assert billed == [5000, 7000, 0, 3000]
    assert sum(billed) == 15_000  # the CLI's own final total


@pytest.mark.anyio
async def test_autonomous_turn_with_nothing_buffered_is_sent(tmp_path, monkeypatch):
    """A finished-jobs digest is an autonomous turn too; with no CLI turn
    waiting it goes to Claude like any other prompt."""
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "one"}], [{"text": "two"}]]})
    try:
        await _stream_text(model)
        digest = "[background jobs finished since your last turn]"
        assert await _stream_text(model, _autonomous(digest)) == "two"
    finally:
        await model.aclose()
    assert _user_texts(tmp_path)[1].startswith("<turn-context")


# --- control sync: mode / model / thinking reach the process -----------------------


def _controls_sent(tmp_path: Path) -> list[dict]:
    """Every non-handshake control request the fake read, oldest first."""
    return [
        m["request"]
        for m in read_claude_log(tmp_path)
        if m.get("type") == "control_request" and m["request"]["subtype"] != "initialize"
    ]


def _levers(requests: list[dict]) -> list[str]:
    return [r["subtype"] for r in requests]


@pytest.mark.anyio
async def test_mode_is_sent_once_per_process_and_again_only_when_it_changes(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    mode = "auto"
    model.mode_getter = lambda: mode
    try:
        await model.request(_user("a"), None, ModelRequestParameters())
        await model.request(_user("b"), None, ModelRequestParameters())
        mode = "ask"  # same CLI mode as auto: the broker tells them apart
        await model.request(_user("c"), None, ModelRequestParameters())
        mode = "plan"
        await model.request(_user("d"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    sent = _controls_sent(tmp_path)
    # The launch --model matches the process, unset thinking matches the CLI's
    # default: only the mode goes out, and only when the CLI-side value moves.
    assert [r for r in sent if r["subtype"] != "get_usage"] == [
        {"subtype": "set_permission_mode", "mode": "default"},
        {"subtype": "set_permission_mode", "mode": "default"},
        {"subtype": "set_permission_mode", "mode": "plan"},
    ]


@pytest.mark.anyio
async def test_a_respawned_process_gets_the_mode_again(tmp_path, monkeypatch):
    model = _model(
        tmp_path,
        monkeypatch,
        {"session_id": "S4", "known_sessions": ["S4"], "turns": [[{"text": "one"}]]},
    )
    model.mode_getter = lambda: "plan"
    try:
        await model.request(_user("a"), None, ModelRequestParameters())
        await model._process.aclose()  # idle reaper / crash stand-in
        await model.request(_user("b"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    modes = [r["mode"] for r in _controls_sent(tmp_path) if r["subtype"] == "set_permission_mode"]
    assert modes == ["plan", "plan"] and len(read_claude_argvs(tmp_path)) == 2


@pytest.mark.anyio
async def test_thinking_reaches_claude_from_settings_then_the_live_level(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})
    level: str | None = None
    model.thinking_getter = lambda: level
    try:
        # Unset everywhere: marim has no opinion, nothing is sent.
        await model.request(_user("a"), None, ModelRequestParameters())
        # The per-turn ModelSettings (the main loop's path) wins ...
        await model.request(_user("b"), {"thinking": "high"}, ModelRequestParameters())
        # ... and an unchanged level costs nothing.
        await model.request(_user("c"), {"thinking": "high"}, ModelRequestParameters())
        # Without settings the live harness level is read (an aux clone path).
        level = "off"
        await model.request(_user("d"), None, ModelRequestParameters())
        # Back to unset: both levers are handed back to the CLI's defaults.
        level = None
        await model.request(_user("e"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    thinking = [
        r
        for r in _controls_sent(tmp_path)
        if r["subtype"] not in {"get_usage", "set_permission_mode"}
    ]
    assert thinking == [
        {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": 32768},
        {"subtype": "apply_flag_settings", "settings": {"effortLevel": "high"}},
        {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": 0},
        {"subtype": "apply_flag_settings", "settings": {"effortLevel": "low"}},
        {"subtype": "set_max_thinking_tokens", "max_thinking_tokens": None},
        {"subtype": "apply_flag_settings", "settings": {"effortLevel": None}},
    ]


@pytest.mark.anyio
async def test_ephemeral_clones_send_no_controls(tmp_path, monkeypatch):
    base = _model(tmp_path, monkeypatch, {"turns": [[{"text": "t"}]]})
    base.thinking_getter = lambda: "high"
    clone = base.ephemeral_clone(cwd=str(tmp_path))
    clone.thinking_getter = base.thinking_getter
    try:
        await clone.request(_user("title this"), {"thinking": "high"}, ModelRequestParameters())
    finally:
        await base.aclose()
    # Read-only comes from the broker's denial, not from Claude's plan mode
    # (whose system prompt would leak into the title); thinking is not a
    # titler's business either.
    assert _levers(_controls_sent(tmp_path)) == []


@pytest.mark.anyio
async def test_release_conversation_makes_the_next_turn_follow_the_rebound_store(
    tmp_path, monkeypatch
):
    """A session switch rebinds the store under the adapter. Without the
    release the live process — session A's Claude conversation — would take
    B's first prompt, and A's id would be persisted over B's ref. With it,
    the next turn resumes what the store now says (here B) on a fresh
    process, and every reading taken off the old process is gone."""
    model = _model(
        tmp_path,
        monkeypatch,
        {
            "session_id": "A",
            "known_sessions": ["B"],
            "turns": [[{"text": "one"}], [{"text": "two"}]],
        },
    )
    ref: dict[str, str | None] = {"v": None}
    model.session_ref_getter = lambda: ref["v"]
    refs: list[str] = []
    model.on_session_ref = refs.append
    try:
        await _stream_text(model, _user("first"))
        assert model.session_id == "A"
        ref["v"] = SESSION_REF_PREFIX + "B"  # the harness switched to session B
        model.release_conversation()
        assert model._process is None and model.context_report is None
        await asyncio.sleep(0.05)  # the scheduled close
        await _stream_text(model, _user("second"))
    finally:
        await model.aclose()
    argvs = read_claude_argvs(tmp_path)
    assert len(argvs) == 2 and argvs[1][argvs[1].index("--resume") + 1] == "B"
    assert "--resume" not in argvs[0]


@pytest.mark.anyio
async def test_release_conversation_with_no_process_is_a_no_op(tmp_path, monkeypatch):
    model = _model(tmp_path, monkeypatch, {})
    model.release_conversation()  # nothing spawned yet: nothing to schedule
    assert model._process is None


@pytest.mark.anyio
async def test_adopt_moves_the_live_process_to_the_new_model(tmp_path, monkeypatch):
    old = _model(tmp_path, monkeypatch, {"session_id": "S9", "turns": [[{"text": "one"}]]})
    await old.request(_user("a"), None, ModelRequestParameters())
    process = old._process
    assert process is not None
    old.context_report = ContextReport(4_000, 200_000)  # a reading the switch must keep
    new = ClaudeCliModel("opus")
    new.cwd, new.mode_getter = old.cwd, old.mode_getter
    try:
        # What Harness.set_model does: adopt, then close the outgoing model.
        new.adopt(old)
        await old.aclose()
        assert old._process is None and old.context_report is None
        assert new._process is process and process.alive
        assert new.context_report == ContextReport(4_000, 200_000)
        resp = await new.request(_user("a") + _user("b"), None, ModelRequestParameters())
    finally:
        await new.aclose()
    assert resp.model_name == "opus"
    # One launch for both models; the switch was a control request, sent
    # once, and the turn went down the resumed process as new text only.
    assert len(read_claude_argvs(tmp_path)) == 1
    assert {"subtype": "set_model", "model": "opus"} in _controls_sent(tmp_path)
    assert _user_texts(tmp_path)[-1] == "b"
    assert process.controls.model == "opus"


@pytest.mark.anyio
async def test_adopt_is_refused_across_ephemeral_or_foreign_models(tmp_path, monkeypatch):
    old = _model(tmp_path, monkeypatch, {"turns": [[{"text": "one"}]]})
    await old.request(_user("a"), None, ModelRequestParameters())
    try:
        clone = old.ephemeral_clone(cwd=str(tmp_path))
        clone.adopt(old)
        assert clone._process is None and old._process is not None
        new = ClaudeCliModel("opus")
        new.adopt(object())  # type: ignore[arg-type]
        new.adopt(new)
        assert new._process is None
    finally:
        await old.aclose()


@pytest.mark.anyio
async def test_a_rejected_model_switch_fails_the_turn_and_keeps_the_process(tmp_path, monkeypatch):
    old = _model(tmp_path, monkeypatch, {"reject_models": ["bogus"], "turns": [[{"text": "one"}]]})
    await old.request(_user("a"), None, ModelRequestParameters())
    new = ClaudeCliModel("bogus")
    new.cwd, new.mode_getter = old.cwd, old.mode_getter
    new.adopt(old)
    try:
        with pytest.raises(CliModelError, match="did not accept the model switch.*bogus"):
            await new.request(_user("a") + _user("b"), None, ModelRequestParameters())
        # The process is still there for the user's next pick; the turn text
        # never went out on the wrong model.
        assert new._process is not None and new._process.alive
        assert new._process.controls.model == "sonnet"
    finally:
        await new.aclose()
    assert _user_texts(tmp_path) == ["User: a"]


@pytest.mark.anyio
async def test_a_rejected_mode_is_logged_and_the_turn_goes_on(tmp_path, monkeypatch, caplog):
    model = _model(tmp_path, monkeypatch, {"turns": [[{"text": "ok"}]]})

    async def refuse(*a, **k):
        raise claude_cli_model.ControlError("Cannot set permission mode")

    try:
        process, _ = await model._ensure_process(_user("a"))
        monkeypatch.setattr(process, "set_mode", refuse)
        with caplog.at_level("WARNING"):
            resp = await model.request(_user("a"), None, ModelRequestParameters())
    finally:
        await model.aclose()
    assert resp.parts[0].content == "ok"
    assert "ignored the mode switch" in caplog.text
