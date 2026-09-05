"""CodexCliModel against the scripted fake app-server (tests/fakes)."""

from __future__ import annotations

import asyncio

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    ModelRequest,
    SystemPromptPart,
    UserPromptPart,
)
from pydantic_ai.models import ModelRequestParameters
from pydantic_ai.settings import ModelSettings

from marim_harness.codex.server import CodexServer
from marim_harness.config.codex_cli_model import CliModelError, CodexCliModel, effort_for
from marim_harness.runtime.permissions import Mode
from tests.fakes import fake_codex_bin, read_request_log

pytestmark = pytest.mark.anyio

PARAMS = ModelRequestParameters(function_tools=[], allow_text_output=True, output_tools=[])


def _msgs(text: str = "hello") -> list:
    return [ModelRequest(parts=[SystemPromptPart(content="SYS"), UserPromptPart(content=text)])]


def _hello_turn(text: str = "Hi there") -> list[dict]:
    return [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": text}},
        {
            "notify": "thread/tokenUsage/updated",
            "params": {
                "tokenUsage": {
                    "total": {"inputTokens": 12, "outputTokens": 5, "cachedInputTokens": 3}
                }
            },
        },
    ]


def _model(tmp_path, scenario: dict, *, model_id="gpt-5.6-sol", mode=Mode.auto) -> CodexCliModel:
    server = CodexServer(binary=fake_codex_bin(tmp_path, scenario))
    m = CodexCliModel(model_id, server=server)
    m.cwd = str(tmp_path)
    m.mode_getter = lambda: mode.value
    return m


def test_effort_for_maps_levels():
    listed = ["low", "medium", "high", "xhigh"]
    assert effort_for(None, listed) is None
    assert effort_for("off", listed) is None
    assert effort_for("minimal", listed) == "low"
    assert effort_for("low", listed) == "low"
    assert effort_for("medium", listed) == "medium"
    assert effort_for("high", listed) == "high"
    assert effort_for("xhigh", listed) == "xhigh"
    assert effort_for("xhigh", ["low", "medium", "high"]) == "high"
    assert effort_for("xhigh", None) == "high"  # unknown catalog: conservative


async def test_request_returns_text_and_usage(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        resp = await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert resp.parts[0].content == "Hi there"
    assert resp.provider_name == "codex-cli" and resp.model_name == "gpt-5.6-sol"
    assert resp.usage.input_tokens == 12 and resp.usage.output_tokens == 5
    assert resp.usage.cache_read_tokens == 3
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["developerInstructions"] == "SYS"
    assert start["params"]["approvalPolicy"] == "on-request"
    assert start["params"]["sandbox"] == "workspace-write"
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["input"] == [{"type": "text", "text": "hello", "text_elements": []}]
    assert turn["params"]["sandboxPolicy"]["type"] == "workspaceWrite"
    assert "effort" not in turn["params"]


async def test_thread_is_reused_across_turns_and_reported(tmp_path):
    refs: list[str] = []
    m = _model(tmp_path, {"turns": [_hello_turn("a"), _hello_turn("b")]})
    m.on_session_ref = refs.append
    try:
        await m.request(_msgs("one"), None, PARAMS)
        await m.request(_msgs("two"), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    assert sum(r["method"] == "thread/start" for r in log) == 1
    assert sum(r["method"] == "turn/start" for r in log) == 2
    assert refs == ["codex-cli:thread-1"]


async def test_resumes_persisted_thread_or_falls_back(tmp_path):
    m = _model(tmp_path, {"resumable": ["thread-9"], "turns": [_hello_turn()]})
    m.session_ref_getter = lambda: "codex-cli:thread-9"
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    assert any(r["method"] == "thread/resume" for r in log)
    assert not any(r["method"] == "thread/start" for r in log)

    # Unknown id -> fresh thread, and the FULL history goes into the first input
    # (a cold start after a resume must not lose the conversation).
    m2 = _model(tmp_path, {"resumable": [], "turns": [_hello_turn()]})
    m2.session_ref_getter = lambda: "codex-cli:thread-gone"
    history = [
        ModelRequest(parts=[UserPromptPart(content="first question")]),
        ModelRequest(parts=[UserPromptPart(content="second question")]),
    ]
    try:
        await m2.request(history, None, PARAMS)
    finally:
        await m2.aclose()
    log = read_request_log(tmp_path)
    turn = [r for r in log if r["method"] == "turn/start"][-1]
    assert "first question" in turn["params"]["input"][0]["text"]
    assert "second question" in turn["params"]["input"][0]["text"]


async def test_foreign_session_ref_is_ignored(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    m.session_ref_getter = lambda: "claude-cli:abc"  # another provider's ref
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert not any(r["method"] == "thread/resume" for r in read_request_log(tmp_path))


async def test_effort_from_model_settings_thinking(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        await m.request(_msgs(), ModelSettings(thinking="xhigh"), PARAMS)  # type: ignore[typeddict-item]
    finally:
        await m.aclose()
    turn = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/start")
    assert turn["params"]["effort"] == "xhigh"  # gpt-5.6-sol lists xhigh in DEFAULT_MODELS


async def test_plan_mode_is_read_only_and_never(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]}, mode=Mode.plan)
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    log = read_request_log(tmp_path)
    start = next(r for r in log if r["method"] == "thread/start")
    assert start["params"]["approvalPolicy"] == "never"
    assert start["params"]["sandbox"] == "read-only"
    turn = next(r for r in log if r["method"] == "turn/start")
    assert turn["params"]["sandboxPolicy"] == {"type": "readOnly", "networkAccess": False}


async def test_headless_folds_tool_activity_into_text(tmp_path):
    turn = [
        {
            "notify": "item/started",
            "params": {
                "item": {
                    "id": "c1",
                    "type": "commandExecution",
                    "command": ["ls", "-la"],
                    "cwd": "/w",
                }
            },
        },
        {
            "notify": "item/completed",
            "params": {
                "item": {
                    "id": "c1",
                    "type": "commandExecution",
                    "command": ["ls", "-la"],
                    "cwd": "/w",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "a.py",
                }
            },
        },
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "Found a.py"}},
    ]
    m = _model(tmp_path, {"turns": [turn]})
    try:
        resp = await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    text = resp.parts[0].content
    assert "▸ bash" in text and "ls -la" in text and text.endswith("Found a.py")


async def test_stream_emits_cards_out_of_band(tmp_path):
    turn = [
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "Look: "}},
        {
            "notify": "item/started",
            "params": {
                "item": {"id": "c1", "type": "commandExecution", "command": "pwd", "cwd": "/w"}
            },
        },
        {
            "notify": "item/completed",
            "params": {
                "item": {
                    "id": "c1",
                    "type": "commandExecution",
                    "command": "pwd",
                    "cwd": "/w",
                    "status": "completed",
                    "exitCode": 0,
                    "aggregatedOutput": "/w",
                }
            },
        },
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m2", "delta": "done"}},
    ]
    m = _model(tmp_path, {"turns": [turn]})
    seen: list = []

    async def on_activity(events):
        seen.extend(events)

    m.on_activity = on_activity
    texts: list[str] = []
    try:
        async with m.request_stream(_msgs(), None, PARAMS) as stream:
            async for ev in stream:
                delta = getattr(getattr(ev, "delta", None), "content_delta", None)
                if delta:
                    texts.append(delta)
            resp = stream.get()
    finally:
        await m.aclose()
    assert "".join(texts) == "Look: done"
    assert [type(e) for e in seen] == [FunctionToolCallEvent, FunctionToolResultEvent]
    assert seen[0].part.tool_name == "bash" and seen[0].part.args == {"command": "pwd", "cwd": "/w"}
    assert seen[1].part.content == "/w"
    # Two prose runs around the card -> two text parts (cards mode), no ▸ line.
    assert len(resp.parts) == 2 and "▸" not in resp.parts[0].content


async def test_failed_turn_raises_cli_model_error(tmp_path):
    m = _model(tmp_path, {"turns": [[{"fail": "quota exhausted"}]]})
    try:
        with pytest.raises(CliModelError, match="quota exhausted"):
            await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()


async def test_server_crash_raises_with_stderr_tail(tmp_path):
    m = _model(tmp_path, {"turns": [[{"exit": 3}]]})
    try:
        with pytest.raises(CliModelError, match="exited mid-turn"):
            await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()


async def test_cancel_interrupts_the_turn(tmp_path):
    m = _model(tmp_path, {"turns": [[{"hang": True}], _hello_turn()]})
    try:
        task = asyncio.create_task(m.request(_msgs(), None, PARAMS))
        await asyncio.sleep(0.3)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        await asyncio.sleep(0.2)
        assert any(r["method"] == "turn/interrupt" for r in read_request_log(tmp_path))
        # The thread survives the interrupt: the next turn reuses it.
        resp = await m.request(_msgs("again"), None, PARAMS)
        assert resp.parts[0].content == "Hi there"
    finally:
        await m.aclose()


async def test_steer_forwards_to_active_turn_only(tmp_path):
    m = _model(tmp_path, {"turns": [[{"hang": True}]]})
    try:
        assert m.steer("nothing running") is False
        task = asyncio.create_task(m.request(_msgs(), None, PARAMS))
        await asyncio.sleep(0.3)
        assert m.steer("also check tests") is True
        await asyncio.sleep(0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        steer = next(r for r in read_request_log(tmp_path) if r["method"] == "turn/steer")
        assert steer["params"]["input"][0]["text"] == "also check tests"
    finally:
        await m.aclose()


async def test_ask_mode_brokers_approval_through_request_approval(tmp_path):
    turn = [
        {
            "request": "item/commandExecution/requestApproval",
            "params": {"itemId": "c1", "command": "rm -rf build", "cwd": "/w"},
            "record_as": "approval",
        },
        {"notify": "item/agentMessage/delta", "params": {"itemId": "m1", "delta": "ok"}},
    ]
    m = _model(tmp_path, {"turns": [turn]}, mode=Mode.ask)
    asked: list = []

    async def approver(call):
        asked.append(call.tool_name)
        return True

    m.request_approval = approver
    try:
        await m.request(_msgs(), None, PARAMS)
    finally:
        await m.aclose()
    assert asked == ["bash"]
    assert any(r.get("approval") == {"decision": "accept"} for r in read_request_log(tmp_path))
    start = next(r for r in read_request_log(tmp_path) if r["method"] == "thread/start")
    assert start["params"]["approvalPolicy"] == "untrusted"


async def test_ephemeral_clone_is_plan_mode_and_threadless(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    clone = m.ephemeral_clone(cwd="/elsewhere")
    assert isinstance(clone, CodexCliModel) and clone.ephemeral
    assert clone.cwd == "/elsewhere" and clone.mode_getter() == "plan"
    assert clone.thread is None and clone.session_ref_getter is None
    assert clone.model_name == m.model_name
    await m.aclose()


async def test_compact_remote_calls_thread_compact(tmp_path):
    m = _model(tmp_path, {"turns": [_hello_turn()]})
    try:
        await m.compact_remote()  # no thread yet: no-op, no error
        await m.request(_msgs(), None, PARAMS)
        await m.compact_remote()
    finally:
        await m.aclose()
    assert any(r["method"] == "thread/compact/start" for r in read_request_log(tmp_path))


async def test_missing_binary_is_a_cli_model_error(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CODEX_CLI_BIN", str(tmp_path / "no-such-codex"))
    m = CodexCliModel("gpt-5.6-sol")
    m.cwd = str(tmp_path)
    with pytest.raises(CliModelError, match="codex"):
        await m.request(_msgs(), None, PARAMS)
