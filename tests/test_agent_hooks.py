import stat
from pathlib import Path

import pytest
from pydantic_ai.messages import (
    FunctionToolResultEvent,
    ModelResponse,
    RetryPromptPart,
    TextPart,
    ToolReturnPart,
)
from pydantic_ai.models.function import FunctionModel

from marim_harness.hooks import events as hook_events
from marim_harness.hooks.runner import HookRunner
from marim_harness.runtime.deps import Deps
from marim_harness.runtime.permissions import Mode
from tests.conftest import (
    _capture_script,
    _edit_then_done_model,
    _make_harness,
    _make_subagent_def,
    _read_hits,
)


@pytest.mark.anyio
async def test_harness_wires_turn_hooks_onto_deps(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)
    harness = _make_harness(_edit_then_done_model(), deps)
    assert deps.services.turn_hooks is harness.hooks


def _hook_script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _prompt_capturing_model(sink: list) -> FunctionModel:
    """Records the LAST user-prompt text it sees per call (the current turn's
    new prompt, not history), then replies 'ok'. pydantic-ai's FunctionModel
    receives the full conversation history each call, so we capture only the
    latest UserPromptPart to isolate the current turn's new prompt.
    Supports both non-streamed and streamed requests (streaming is required when
    an event_stream_handler is set, e.g. when hooks are configured)."""
    def fn(messages, info):
        latest = None
        for msg in messages:
            for part in getattr(msg, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str) and part.__class__.__name__ == "UserPromptPart":
                    latest = content
        if latest is not None:
            sink.append(latest)
        return ModelResponse(parts=[TextPart(content="ok")])

    async def stream_fn(messages, info):
        latest = None
        for msg in messages:
            for part in getattr(msg, "parts", []):
                content = getattr(part, "content", None)
                if isinstance(content, str) and part.__class__.__name__ == "UserPromptPart":
                    latest = content
        if latest is not None:
            sink.append(latest)
        yield "ok"

    return FunctionModel(fn, stream_function=stream_fn)


@pytest.mark.anyio
async def test_session_start_context_is_prepended_once(tmp_path):
    cmd = _hook_script(tmp_path, "ss.sh", "echo SESSION_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.session_start("startup")
    await harness.run_turn("first")
    assert "SESSION_CTX" in sink[0]
    await harness.run_turn("second")
    assert "SESSION_CTX" not in sink[1]  # consumed; not repeated


@pytest.mark.anyio
async def test_user_prompt_submit_context_is_prepended(tmp_path):
    cmd = _hook_script(tmp_path, "ups.sh", "echo PROMPT_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.USER_PROMPT_SUBMIT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("do the thing")
    assert "PROMPT_CTX" in sink[0]
    assert "do the thing" in sink[0]


@pytest.mark.anyio
async def test_user_prompt_submit_fires_on_every_turn(tmp_path):
    """UserPromptSubmit hook fires on every turn, not just the first. The hook
    context is prepended to each turn's prompt, proving repeated activation."""
    cmd = _hook_script(tmp_path, "ups_every.sh", "echo PROMPT_CTX\n")
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.USER_PROMPT_SUBMIT: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("first")
    await harness.run_turn("second")
    assert "PROMPT_CTX" in sink[0]
    assert "PROMPT_CTX" in sink[1]  # fires again on the second turn, not one-shot


@pytest.mark.anyio
async def test_no_hooks_runs_turn_normally(tmp_path):
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # hooks=None
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    out = await harness.run_turn("hello")
    assert out == "ok"
    assert sink[0] == "hello"  # untouched


def test_strip_turn_context_recovers_typed_text():
    from marim_harness.runtime.harness import strip_turn_context, wrap_turn_context

    wrapped = wrap_turn_context("<agentmemory-context>stuff</agentmemory-context>",
                                "implement a fetch tool")
    assert strip_turn_context(wrapped) == "implement a fetch tool"
    # Multi-line typed text survives intact.
    wrapped2 = wrap_turn_context("ctx", "line one\n\nline two")
    assert strip_turn_context(wrapped2) == "line one\n\nline two"


def test_strip_turn_context_passes_through_plain_prompt():
    from marim_harness.runtime.harness import strip_turn_context

    # No envelope -> returned unchanged, even if it mentions the tag in prose.
    assert strip_turn_context("just a normal prompt") == "just a normal prompt"
    assert strip_turn_context("talk about <turn-context> as a topic") == (
        "talk about <turn-context> as a topic"
    )


@pytest.mark.anyio
async def test_injected_context_is_wrapped_so_replay_can_recover_typed_text(tmp_path):
    """A SessionStart hook injects context that gets prepended to the prompt.
    The persisted UserPromptPart must wrap it in a turn-context envelope so a
    resumed session can recover just what the user typed, while the model still
    sees the injected context."""
    from marim_harness.runtime.harness import strip_turn_context

    cmd = _hook_script(tmp_path, "ss.sh", "echo SESSION_CTX\n")
    hooks = HookRunner(
        {hook_events.SESSION_START: [{"hooks": [{"type": "command", "command": cmd}]}]}
    )
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=hooks)
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.session_start("startup")
    await harness.run_turn("implement a fetch tool")

    # The model still sees the injected context this turn.
    assert "SESSION_CTX" in sink[0]
    # The persisted prompt wraps it so replay can strip back to the typed text.
    persisted = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert persisted, "expected a persisted user prompt"
    assert "SESSION_CTX" in persisted[0]  # context is in the stored prompt
    assert strip_turn_context(persisted[0]) == "implement a fetch tool"


@pytest.mark.anyio
async def test_plain_turn_is_not_wrapped(tmp_path):
    """With nothing injected, the persisted prompt is the typed text verbatim —
    no envelope — so existing sessions and output are unaffected."""
    from marim_harness.runtime.harness import strip_turn_context

    deps = Deps(workspace_root=tmp_path, mode=Mode.auto)  # no hooks
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    await harness.run_turn("hello")
    persisted = [
        p.content
        for m in harness.session.history
        for p in getattr(m, "parts", [])
        if type(p).__name__ == "UserPromptPart"
    ]
    assert persisted[0] == "hello"
    assert strip_turn_context(persisted[0]) == "hello"


@pytest.mark.anyio
async def test_pre_and_post_tool_use_fire(tmp_path):
    (tmp_path / "a.txt").write_text("foo")
    log = tmp_path / "toolhooks.log"
    # Write a Python helper script that the hook shell script will invoke; this
    # avoids bash single-quote escaping issues when embedding the log path.
    helper = tmp_path / "loghook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write("
        f"d['hook_event_name'] + ' ' + d.get('tool_name', '') + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "tool.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.PRE_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
        hook_events.POST_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")
    lines = log.read_text().splitlines()
    assert "PreToolUse edit_file" in lines
    assert any(line.startswith("PostToolUse edit_file") for line in lines)


@pytest.mark.anyio
async def test_post_tool_use_includes_tool_input(tmp_path):
    """PostToolUse payload must carry tool_input (the args) correlated from the
    matching PreToolUse call so that CC plugin scripts can read the call's
    input to correlate it with its result (CC-contract fidelity)."""
    (tmp_path / "a.txt").write_text("foo")
    log = tmp_path / "toolinput.log"
    # Python helper logs the full JSON payload for PostToolUse events only.
    helper = tmp_path / "loginput.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"if d.get('hook_event_name') == 'PostToolUse':\n"
        f"    open({str(log)!r}, 'a').write(json.dumps(d) + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "toolinput.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.PRE_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
        hook_events.POST_TOOL_USE: [
            {"matcher": "*", "hooks": [{"type": "command", "command": cmd}]}
        ],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.run_turn("change foo to bar")

    # The log file must exist and contain at least one PostToolUse line.
    assert log.exists(), "No PostToolUse hook fired"
    import json as _json
    post_payloads = [_json.loads(line) for line in log.read_text().splitlines() if line.strip()]
    assert post_payloads, "No PostToolUse payloads logged"

    # Every PostToolUse payload must carry tool_input with the actual args.
    for payload in post_payloads:
        assert "tool_input" in payload, (
            f"PostToolUse payload missing tool_input: {payload}"
        )
    # Specifically: the edit_file call's args (path + edits) must be present.
    edit_payloads = [p for p in post_payloads if p.get("tool_name") == "edit_file"]
    assert edit_payloads, "No PostToolUse for edit_file found"
    assert edit_payloads[0]["tool_input"].get("path") == "a.txt", (
        f"Expected tool_input.path == 'a.txt', got: {edit_payloads[0]['tool_input']}"
    )


@pytest.mark.anyio
async def test_subagent_start_and_stop_fire(tmp_path):
    _make_subagent_def(tmp_path)
    log = tmp_path / "sub.log"
    # Use a Python helper file to avoid bash single-quote escaping issues when
    # embedding the log path (same pattern as test_pre_and_post_tool_use_fire).
    helper = tmp_path / "subhook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(d['hook_event_name'] + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "sub.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.SUBAGENT_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SUBAGENT_STOP: [{"hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    # A streaming-capable model the sub-agent will run: just reply 'sub-done'.
    # Hooks make the sub-agent run stream (so its tool calls hit the engine), so
    # a non-streaming FunctionModel can't back it — same discipline as the main
    # agent's hooked turns (see test_stop_fires_at_turn_end).
    from pydantic_ai.models.test import TestModel
    harness = _make_harness(
        TestModel(call_tools=[], custom_output_text="sub-done"), deps
    )
    out = await harness.subagents.run("helper", "do a thing", "stream-1")
    assert "sub-done" in out
    lines = log.read_text().splitlines()
    assert "SubagentStart" in lines
    assert "SubagentStop" in lines


@pytest.mark.anyio
async def test_stop_fires_at_turn_end(tmp_path):
    log = tmp_path / "stop.log"
    cmd = _hook_script(tmp_path, "stop.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner(
                    {hook_events.STOP: [{"hooks": [{"type": "command", "command": cmd}]}]}
                ))
    # Use a streaming-capable model: hooks configure a hooked_handler that forces
    # streaming mode (same discipline as test_pre_and_post_tool_use_fire).
    sink: list = []
    harness = _make_harness(_prompt_capturing_model(sink), deps)
    out = await harness.run_turn("anything")
    assert out == "ok"
    assert '"hook_event_name": "Stop"' in log.read_text()


@pytest.mark.anyio
async def test_session_end_fires(tmp_path):
    log = tmp_path / "end.log"
    cmd = _hook_script(tmp_path, "end.sh", f"cat >> {log}\n")
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto,
                hooks=HookRunner(
                    {hook_events.SESSION_END: [{"hooks": [{"type": "command", "command": cmd}]}]}
                ))
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])), deps
    )
    await harness.session_end("exit")
    assert '"hook_event_name": "SessionEnd"' in log.read_text()
    assert '"reason": "exit"' in log.read_text()


@pytest.mark.anyio
async def test_background_subagent_start_and_stop_fire(tmp_path):
    _make_subagent_def(tmp_path)
    log = tmp_path / "bg_sub.log"
    # Use a Python helper file to avoid bash single-quote escaping issues when
    # embedding the log path (same pattern as test_pre_and_post_tool_use_fire).
    helper = tmp_path / "bghook.py"
    helper.write_text(
        f"import sys, json\n"
        f"d = json.load(sys.stdin)\n"
        f"open({str(log)!r}, 'a').write(d['hook_event_name'] + '\\n')\n",
        encoding="utf-8",
    )
    cmd = _hook_script(
        tmp_path, "bgsub.sh",
        f"python3 {str(helper)}\n",
    )
    runner = HookRunner({
        hook_events.SUBAGENT_START: [{"hooks": [{"type": "command", "command": cmd}]}],
        hook_events.SUBAGENT_STOP: [{"hooks": [{"type": "command", "command": cmd}]}],
    })
    deps = Deps(workspace_root=tmp_path, mode=Mode.auto, hooks=runner)
    # A streaming-capable model the sub-agent will run: just reply 'bg-done'.
    # (See the foreground test above — hooks force the sub-agent run to stream.)
    from pydantic_ai.models.test import TestModel
    harness = _make_harness(
        TestModel(call_tools=[], custom_output_text="bg-done"), deps
    )
    out = await harness.subagents.run_background("helper", "do a thing")
    assert "bg-done" in out
    lines = log.read_text().splitlines()
    assert "SubagentStart" in lines
    assert "SubagentStop" in lines


@pytest.mark.anyio
async def test_tool_failure_fires_post_tool_use_failure(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "fail.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.POST_TOOL_USE_FAILURE: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    ev = FunctionToolResultEvent(
        part=RetryPromptPart(content="boom", tool_name="edit_file", tool_call_id="tc1")
    )
    await harness.hooks.tool_event(ev, {"tc1": {"path": "a.txt"}})
    hits = _read_hits(out)
    assert len(hits) == 1
    assert hits[0]["hook_event_name"] == "PostToolUseFailure"
    assert hits[0]["tool_name"] == "edit_file"
    assert hits[0]["tool_input"] == {"path": "a.txt"}
    assert "boom" in hits[0]["error"]


@pytest.mark.anyio
async def test_notification_dispatch_payload(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "n.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.hooks.notification("ask_user", "Question from agent", "pick one")
    hits = _read_hits(out)
    assert hits[0]["hook_event_name"] == "Notification"
    assert hits[0]["notification_type"] == "ask_user"
    assert hits[0]["title"] == "Question from agent"
    assert hits[0]["message"] == "pick one"


@pytest.mark.anyio
async def test_approval_round_fires_notification_in_ask_mode(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "appr.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.ask,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )

    async def _approve(call):
        return True

    deps.request_approval = _approve
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.run_turn("edit it")
    hits = [h for h in _read_hits(out) if h["hook_event_name"] == "Notification"]
    assert any(h["notification_type"] == "approval_needed" for h in hits)
    assert any("edit_file" in h["message"] for h in hits)


@pytest.mark.anyio
async def test_auto_mode_does_not_fire_approval_notification(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "noappr.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.NOTIFICATION: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.run_turn("edit it")
    hits = [h for h in _read_hits(out)
            if h.get("notification_type") == "approval_needed"]
    assert hits == []


@pytest.mark.anyio
async def test_tool_success_fires_post_tool_use_not_failure(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "ok.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner({
            hook_events.POST_TOOL_USE: [{"hooks": [{"type": "command", "command": cmd}]}],
            hook_events.POST_TOOL_USE_FAILURE: [{"hooks": [{"type": "command", "command": cmd}]}],
        }),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    ev = FunctionToolResultEvent(
        part=ToolReturnPart(tool_name="read_file", content="ok", tool_call_id="tc2")
    )
    await harness.hooks.tool_event(ev, {"tc2": {"path": "a.txt"}})
    hits = _read_hits(out)
    assert len(hits) == 1
    assert hits[0]["hook_event_name"] == "PostToolUse"


@pytest.mark.anyio
async def test_task_completed_dispatch_payload(tmp_path):
    out = tmp_path / "hits.jsonl"
    cmd = _capture_script(tmp_path, "tc.sh", out)
    deps = Deps(
        workspace_root=tmp_path, mode=Mode.auto,
        hooks=HookRunner(
            {hook_events.TASK_COMPLETED: [{"hooks": [{"type": "command", "command": cmd}]}]}
        ),
    )
    harness = _make_harness(_edit_then_done_model(), deps)
    await harness.session_start("startup")
    await harness.hooks.task_completed(task_subject="ship it")
    hits = _read_hits(out)
    assert hits[0]["hook_event_name"] == "TaskCompleted"
    assert hits[0]["task_subject"] == "ship it"
