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

from marim_harness.claude.env import CLI_BINARY_ENV
from marim_harness.claude.protocol import CLOSED
from marim_harness.config.claude_cli_model import (
    SESSION_REF_PREFIX,
    ClaudeCliModel,
    DoneChunk,
    InitChunk,
    TextChunk,
    ThinkingChunk,
    ToolResultChunk,
    ToolUseChunk,
    consume_cli_stream,
    extract_system,
    flatten_history,
    format_activity_line,
    latest_user_text,
    request_usage_from_cli,
)
from marim_harness.config.external_cli import CliModelError
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


def test_stream_renderer_on_cli_activity_dispatches_each_event(monkeypatch):
    # StreamRenderer.on_cli_activity must route every side-channel event through
    # the same dispatch path as the main turn, into a top-level sink.
    import asyncio
    from types import SimpleNamespace

    from marim_harness.interfaces.tui.stream_render import StreamRenderer

    r = StreamRenderer(app=SimpleNamespace())
    dispatched = []

    async def fake_dispatch(event, sink):
        dispatched.append((event, type(sink).__name__))

    monkeypatch.setattr(r, "dispatch_stream_event", fake_dispatch)
    monkeypatch.setattr(r, "app", SimpleNamespace(query_one=lambda *a, **k: object()))

    asyncio.run(r.on_cli_activity(["e1", "e2", "e3"]))
    assert [e for e, _ in dispatched] == ["e1", "e2", "e3"]
    assert all(sink == "_TopLevelSink" for _, sink in dispatched)


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
        text = await _stream_text(model)
    finally:
        await model.aclose()
    spawn_names = [e.part.tool_name for e in activity if hasattr(e, "part")]
    assert spawn_names.count("spawn_agent") == 2
    assert sub_events and all(sid == "tsub" for sid, _, _ in sub_events)
    assert any(u is not None and u.output_tokens == 2 for _, _, u in sub_events)
    assert ("tsub", "claude-haiku-4-5") in sub_models
    assert text == "Four."  # the child's "4" (delta AND block) never entered the main text


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
