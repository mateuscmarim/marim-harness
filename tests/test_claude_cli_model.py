
import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    SystemPromptPart,
    TextPart,
    UserPromptPart,
)

from marim_harness.config.claude_cli_model import (
    ClaudeCliModel,
    CliModelError,
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


@pytest.fixture(autouse=True)
def _fake_cli_binary(monkeypatch):
    monkeypatch.setattr(
        "marim_harness.subagents.cli_backend.resolve_cli_binary",
        lambda: "/usr/bin/claude",
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


def test_flatten_history_renders_tool_calls_and_returns():
    # A history produced by ANOTHER provider (e.g. openrouter using marim's tools)
    # carries ToolCallPart/ToolReturnPart. When claude-cli is switched in mid-session
    # and cold-starts, flatten_history must preserve that tool context, not drop it.
    from pydantic_ai.messages import ToolCallPart, ToolReturnPart

    msgs = [
        ModelRequest(parts=[UserPromptPart(content="what's in config.py?")]),
        ModelResponse(
            parts=[
                TextPart(content="Let me read it."),
                ToolCallPart(
                    tool_name="read_file", args={"path": "config.py"}, tool_call_id="t1"
                ),
            ]
        ),
        ModelRequest(
            parts=[
                ToolReturnPart(
                    tool_name="read_file", content="PORT = 8080", tool_call_id="t1"
                )
            ]
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


def test_ephemeral_model_never_resumes_and_always_sends_system():
    # Aux agents (titler/summarizer) run on an ephemeral model so they never
    # resume — or pollute — the user's live Claude session, and always carry their
    # own instructions. Even with a session_id set, argv must omit --resume and
    # include --append-system-prompt.
    model = ClaudeCliModel("sonnet", ephemeral=True)
    model.mode_getter = lambda: "plan"
    model.session_id = "MAIN-123"  # would normally trigger the resume path
    argv = model._argv(
        [
            __import__("pydantic_ai.messages", fromlist=["ModelRequest"]).ModelRequest(
                parts=[UserPromptPart(content="hey")], instructions="TITLE RULES"
            )
        ]
    )
    assert "--resume" not in argv
    assert "--append-system-prompt" in argv


@pytest.mark.anyio
async def test_ephemeral_model_does_not_store_session_id():
    model = ClaudeCliModel("sonnet", ephemeral=True)
    model.mode_getter = lambda: "plan"

    def _spawn(argv, cwd):
        async def gen():
            yield {"type": "system", "session_id": "S9"}
            yield {
                "type": "assistant",
                "message": {"content": [{"type": "text", "text": "Fix parser"}]},
            }
            yield {
                "type": "result",
                "result": "Fix parser",
                "session_id": "S9",
                "usage": {"input_tokens": 1, "output_tokens": 2},
            }
        return gen()

    model.spawn = _spawn
    from pydantic_ai.messages import ModelRequest
    from pydantic_ai.models import ModelRequestParameters

    await model.request(
        [ModelRequest(parts=[UserPromptPart(content="hi")], instructions="T")],
        None,
        ModelRequestParameters(),
    )
    assert model.session_id is None  # ephemeral: stays stateless across calls


def test_ephemeral_clone_is_stateless_read_only_with_cwd():
    main = ClaudeCliModel("opus")
    main.cwd = "/main"
    clone = main.ephemeral_clone(cwd="/ws")
    assert clone.ephemeral is True
    assert clone._model_id == "opus"
    assert clone.cwd == "/ws"
    assert clone.mode_getter() == "plan"  # aux agents run read-only


@pytest.mark.anyio
async def test_consume_stream_separates_text_and_activity():
    # Folded tool-activity lines must not collide with the surrounding text — each
    # segment gets its own line so the TUI renders readable output, not a run-on wall.
    objs = [
        {"type": "system", "session_id": "S"},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Surveying. "}]}},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "name": "Bash", "input": {"command": "ls | head -50"}}
        ]}},
        {"type": "assistant", "message": {"content": [{"type": "text", "text": "Now sizes."}]}},
        {"type": "result", "result": "Now sizes.", "session_id": "S",
         "usage": {"input_tokens": 1, "output_tokens": 1}},
    ]

    async def gen():
        for o in objs:
            yield o

    text = ""
    async for chunk in consume_cli_stream(gen()):
        if isinstance(chunk, TextChunk):
            text += chunk.delta
    # The bug: "ls | head -50Now sizes." ran together. The activity line and the
    # following text must be separated by a newline.
    assert "head -50Now" not in text
    assert "⏺ Bash ls | head -50" in text
    # The text after the tool activity begins on a fresh line.
    assert "\nNow sizes." in text


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
    # Segments are blank-line separated; the final text segment carries that lead.
    assert texts[-1].strip() == "Done."
    assert texts[-1].startswith("\n\n")
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


def _fake_objs(objs):
    async def _spawn(argv, cwd):
        for o in objs:
            yield o

    return _spawn


_INIT = {"type": "system", "subtype": "init", "session_id": "S1", "model": "claude-x"}


def _result(text, sid="S1"):
    return {
        "type": "result",
        "result": text,
        "session_id": sid,
        "usage": {"input_tokens": 1, "output_tokens": 2},
        "total_cost_usd": 0.0,
    }


def _user(text):
    from pydantic_ai.messages import ModelRequest, UserPromptPart

    return [ModelRequest(parts=[UserPromptPart(content=text)], instructions="SYS")]


@pytest.mark.anyio
async def test_request_returns_text_only_response_and_captures_session():
    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [
            _INIT,
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "hello"}]}},
            _result("hello"),
        ]
    )
    from pydantic_ai.messages import TextPart, ToolCallPart
    from pydantic_ai.models import ModelRequestParameters

    resp = await model.request(_user("hi"), None, ModelRequestParameters())
    assert [type(p) for p in resp.parts] == [TextPart]
    assert resp.parts[0].content == "hello"
    assert not any(isinstance(p, ToolCallPart) for p in resp.parts)
    assert model.session_id == "S1"  # captured for the next turn


@pytest.mark.anyio
async def test_second_turn_uses_resume(monkeypatch):
    model = ClaudeCliModel("sonnet")
    model.session_id = "S1"
    captured = {}

    def _spawn(argv, cwd):
        captured["argv"] = argv

        async def gen():
            yield _INIT
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
            yield _result("ok", sid="S1")

        return gen()

    model.spawn = _spawn
    from pydantic_ai.models import ModelRequestParameters

    await model.request(_user("again"), None, ModelRequestParameters())
    assert "--resume" in captured["argv"]
    assert captured["argv"][captured["argv"].index("--resume") + 1] == "S1"
    # The latest user message is the positional prompt, not the flattened history.
    assert "again" in captured["argv"]


@pytest.mark.anyio
async def test_request_raises_on_incomplete_stream():
    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}]
    )
    from pydantic_ai.models import ModelRequestParameters

    with pytest.raises(CliModelError):
        await model.request(_user("hi"), None, ModelRequestParameters())


@pytest.mark.parametrize("model_id", ["", None])
def test_argv_omits_model_flag_when_id_blank(model_id):
    # A blank/None model id (claude-cli's "let the CLI choose" default) must NOT
    # emit `--model` — otherwise we'd spawn `claude --model None`.
    model = ClaudeCliModel(model_id)
    argv = model._argv(_user("hi"))
    assert "--model" not in argv
    # Sanity: an explicit id still passes through.
    assert "--model" in ClaudeCliModel("sonnet")._argv(_user("hi"))


@pytest.mark.anyio
async def test_request_uses_configured_cwd():
    model = ClaudeCliModel("sonnet")
    model.cwd = "/some/dir"
    captured = {}

    def _spawn(argv, cwd):
        captured["cwd"] = cwd

        async def gen():
            yield _INIT
            yield {"type": "assistant", "message": {"content": [{"type": "text", "text": "ok"}]}}
            yield _result("ok")

        return gen()

    model.spawn = _spawn
    from pydantic_ai.models import ModelRequestParameters

    await model.request(_user("hi"), None, ModelRequestParameters())
    assert captured["cwd"] == "/some/dir"


@pytest.mark.anyio
async def test_request_stream_raises_on_incomplete_stream():
    # The streamed path must fail (not present truncated text as success) when the
    # stream ends without a `result` event — matching request().
    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [{"type": "assistant", "message": {"content": [{"type": "text", "text": "x"}]}}]
    )
    from pydantic_ai.models import ModelRequestParameters

    with pytest.raises(CliModelError):
        async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
            async for _ in stream:
                pass


@pytest.mark.anyio
async def test_request_stream_yields_text_events():
    from pydantic_ai.messages import PartDeltaEvent, PartStartEvent
    from pydantic_ai.models import ModelRequestParameters

    model = ClaudeCliModel("sonnet")
    model.spawn = _fake_objs(
        [
            _INIT,
            {"type": "assistant", "message": {"content": [{"type": "text", "text": "streamed"}]}},
            _result("streamed"),
        ]
    )
    events = []
    async with model.request_stream(_user("hi"), None, ModelRequestParameters()) as stream:
        async for ev in stream:
            events.append(ev)
        final = stream.get()
    assert any(isinstance(e, (PartStartEvent, PartDeltaEvent)) for e in events)
    assert final.parts[0].content == "streamed"
    assert model.session_id == "S1"
