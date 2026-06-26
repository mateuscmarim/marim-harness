import stat
import sys
from typing import cast

import pytest
from pydantic_ai.messages import (
    FunctionToolCallEvent,
    FunctionToolResultEvent,
    PartDeltaEvent,
    PartStartEvent,
    TextPartDelta,
    ToolReturnPart,
)
from pydantic_ai.usage import RunUsage

from marim_harness.subagents_cli import (
    CLI_BINARY_ENV,
    ClaudeCliRunner,
    CliStreamTranslator,
    build_cli_argv,
    cli_permission_mode,
    map_tools_to_cc,
    resolve_cli_binary,
    synth_usage,
)
from marim_harness.tools.names import READ_TOOLS, SUBAGENT_TOOLS


def test_permission_mode_maps_to_auto_and_plan():
    assert cli_permission_mode(True) == "acceptEdits"
    assert cli_permission_mode(False) == "plan"


def test_tool_map_drops_unmapped_and_sorts():
    # READ_TOOLS = read_file, glob, tree, grep + LSP tools. Only read_file/glob/grep
    # map; tree and LSP names are dropped.
    assert map_tools_to_cc(READ_TOOLS) == ["Glob", "Grep", "Read"]
    assert map_tools_to_cc(SUBAGENT_TOOLS) == [
        "Bash", "Edit", "Glob", "Grep", "Read", "WebFetch", "WebSearch", "Write",
    ]


def test_build_argv_includes_required_flags():
    argv = build_cli_argv(
        "/usr/bin/claude", "do the task", "You are a worker.",
        "acceptEdits", ["Read", "Edit"], "opus",
    )
    assert argv[:3] == ["/usr/bin/claude", "-p", "do the task"]
    assert "--output-format" in argv and "stream-json" in argv and "--verbose" in argv
    assert argv[argv.index("--append-system-prompt") + 1] == "You are a worker."
    assert argv[argv.index("--permission-mode") + 1] == "acceptEdits"
    assert argv[argv.index("--allowedTools") + 1] == "Read,Edit"
    assert argv[argv.index("--model") + 1] == "opus"


def test_build_argv_omits_model_and_tools_when_absent():
    argv = build_cli_argv("claude", "t", "s", "plan", [], None)
    assert "--model" not in argv
    assert "--allowedTools" not in argv


def test_resolve_binary_prefers_env(monkeypatch, tmp_path):
    fake = tmp_path / "myclaude"
    fake.write_text("#!/bin/sh\n")
    fake.chmod(0o755)
    monkeypatch.setenv(CLI_BINARY_ENV, str(fake))
    assert resolve_cli_binary() == str(fake)


def test_resolve_binary_none_when_missing(monkeypatch):
    monkeypatch.setenv(CLI_BINARY_ENV, "definitely-not-a-real-binary-xyz")
    assert resolve_cli_binary() is None


def test_synth_usage_maps_token_fields():
    u = synth_usage(
        {"input_tokens": 10, "output_tokens": 5,
         "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1},
        num_turns=3,
    )
    assert isinstance(u, RunUsage)
    assert u.input_tokens == 10 and u.output_tokens == 5
    assert u.cache_read_tokens == 2 and u.cache_write_tokens == 1
    assert u.requests == 3


def test_synth_usage_captures_billed_cost():
    from marim_harness.usage import COST_DETAIL_KEY, exact_cost

    u = synth_usage({"input_tokens": 10, "output_tokens": 5}, num_turns=1,
                    total_cost_usd=0.001)
    # Stored as integer micro-USD so exact_cost() returns the billed amount.
    assert u.details.get(COST_DETAIL_KEY) == 1000
    assert exact_cost(u) == pytest.approx(0.001, rel=1e-6)


def test_synth_usage_omits_cost_key_when_absent():
    from marim_harness.usage import COST_DETAIL_KEY

    u = synth_usage({"input_tokens": 5, "output_tokens": 2}, num_turns=1)
    assert COST_DETAIL_KEY not in u.details


def test_synth_usage_tolerates_none():
    u = synth_usage(None, num_turns=0)
    assert u.input_tokens == 0 and u.output_tokens == 0


def test_translate_assistant_text_emits_start_then_full_delta():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [{"type": "text", "text": "Hello there"}]},
    })
    assert isinstance(events[0], PartStartEvent)
    assert isinstance(events[1], PartDeltaEvent)
    assert isinstance(events[1].delta, TextPartDelta)
    assert events[1].delta.content_delta == "Hello there"
    # start and its delta share the same part index
    assert events[0].index == events[1].index


def test_translate_tool_use_emits_call_event():
    t = CliStreamTranslator()
    events = t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {"path": "x.py"}},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolCallEvent)
    assert ev.part.tool_name == "Read"
    assert ev.part.tool_call_id == "toolu_1"
    assert ev.part.args_as_dict() == {"path": "x.py"}


def test_translate_tool_result_labels_from_prior_call_and_marks_failure():
    t = CliStreamTranslator()
    t.translate({
        "type": "assistant",
        "message": {"content": [
            {"type": "tool_use", "id": "toolu_9", "name": "Bash", "input": {"command": "ls"}},
        ]},
    })
    events = t.translate({
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "toolu_9",
             "content": [{"type": "text", "text": "boom"}], "is_error": True},
        ]},
    })
    assert len(events) == 1
    ev = events[0]
    assert isinstance(ev, FunctionToolResultEvent)
    part = cast(ToolReturnPart, ev.part)
    assert part.tool_name == "Bash"          # carried from the matching call
    assert part.tool_call_id == "toolu_9"
    assert part.content == "boom"            # list-of-blocks flattened to text
    assert part.outcome == "failed"          # is_error → failed


def test_translate_ignores_system_and_result():
    t = CliStreamTranslator()
    assert t.translate({"type": "system", "subtype": "init"}) == []
    assert t.translate({"type": "result", "result": "done"}) == []


# ---------------------------------------------------------------------------
# ClaudeCliRunner — fake-binary integration tests
# ---------------------------------------------------------------------------

_FAKE_CLI = '''#!{python}
import json, sys
lines = [
    {{"type": "system", "subtype": "init"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "Working on it"}},
        {{"type": "tool_use", "id": "toolu_1", "name": "Read", "input": {{"path": "x"}}}},
    ]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "toolu_1",
         "content": "file body", "is_error": False}},
    ]}}}},
    {{"type": "result", "subtype": "success", "result": "Done: found it",
      "num_turns": 2, "total_cost_usd": 0.001,
      "usage": {{"input_tokens": 10, "output_tokens": 5,
                 "cache_read_input_tokens": 2, "cache_creation_input_tokens": 1}}}},
]
for o in lines:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _make_fake_cli(tmp_path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_runner_streams_events_and_returns_result(tmp_path):
    binary = _make_fake_cli(tmp_path)
    seen = []

    async def on_event(stream_id, event, usage):
        seen.append((stream_id, type(event).__name__))

    runner = ClaudeCliRunner(on_event, None)
    result = await runner.run(
        binary=binary, prompt="go", system_prompt="be a worker",
        cwd=str(tmp_path), allow_gated=True, allowed_tools=frozenset({"read_file"}),
        model=None, stream_id="s1",
    )
    assert result.output == "Done: found it"
    assert result.usage.input_tokens == 10 and result.usage.output_tokens == 5
    names = [n for _, n in seen]
    assert "FunctionToolCallEvent" in names
    assert "FunctionToolResultEvent" in names
    assert all(sid == "s1" for sid, _ in seen)


_FAKE_CLI_WITH_MODEL = '''#!{python}
import json, sys
for o in [
    {{"type": "system", "subtype": "init", "model": "claude-opus-4-8[1m]"}},
    {{"type": "assistant", "message": {{"model": "claude-opus-4-8",
        "content": [{{"type": "text", "text": "hi"}}]}}}},
    {{"type": "result", "subtype": "success", "result": "ok",
      "num_turns": 1, "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


@pytest.mark.anyio
async def test_runner_surfaces_real_model_from_init_event(tmp_path):
    p = tmp_path / "fake_claude_model.py"
    p.write_text(_FAKE_CLI_WITH_MODEL.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    models = []

    async def on_model(stream_id, model):
        models.append((stream_id, model))

    runner = ClaudeCliRunner(None, None, on_model)
    result = await runner.run(
        binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
        allow_gated=False, allowed_tools=frozenset(), model=None, stream_id="s1",
    )
    assert result.output == "ok"
    # Surfaced exactly once, from the system/init event, tagged with the stream id.
    assert models == [("s1", "claude-opus-4-8[1m]")]


@pytest.mark.anyio
async def test_runner_skips_model_callback_without_stream_id(tmp_path):
    p = tmp_path / "fake_claude_model2.py"
    p.write_text(_FAKE_CLI_WITH_MODEL.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    models = []

    async def on_model(stream_id, model):
        models.append(model)

    # No stream_id (headless background) -> nothing to address, so no model push.
    runner = ClaudeCliRunner(None, None, on_model)
    await runner.run(
        binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
        allow_gated=False, allowed_tools=frozenset(), model=None, stream_id="",
    )
    assert models == []


@pytest.mark.anyio
async def test_runner_raises_when_no_result(tmp_path):
    p = tmp_path / "silent.py"
    p.write_text(f"#!{sys.executable}\nimport sys; sys.exit(3)\n", encoding="utf-8")
    p.chmod(0o755)
    runner = ClaudeCliRunner(None, None)
    with pytest.raises(Exception) as exc:
        await runner.run(
            binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
            allow_gated=False, allowed_tools=frozenset(), model=None, stream_id="",
        )
    assert "no result" in str(exc.value).lower()


_FAKE_CLI_LARGE_STDERR = '''#!{python}
import json, sys

# Write a large blob to stderr BEFORE writing the stdout result line.
# If the parent drains stdout to EOF before reading stderr, the child will
# block here once the OS pipe buffer (~64 KB) plus asyncio's internal
# StreamReader buffer (~128 KB) are both full — deadlock. 2 MB comfortably
# exceeds both buffers so the deadlock is deterministic.
sys.stderr.write("x" * 2_000_000)
sys.stderr.flush()

result = {{
    "type": "result",
    "subtype": "success",
    "result": "ok after big stderr",
    "num_turns": 1,
    "usage": {{"input_tokens": 1, "output_tokens": 1}},
}}
sys.stdout.write(json.dumps(result) + "\\n")
sys.stdout.flush()
'''


_FAKE_CLI_SLEEPY = '''#!{python}
import json, os, sys, time

pidfile = os.environ.get("FAKE_CLI_PIDFILE", "")
if pidfile:
    with open(pidfile, "w") as f:
        f.write(str(os.getpid()))
        f.flush()

event = {{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "working on it"}},
]}}}}
sys.stdout.write(json.dumps(event) + "\\n")
sys.stdout.flush()

time.sleep(30)
'''


@pytest.mark.anyio
async def test_runner_kills_subprocess_when_event_callback_raises(tmp_path, monkeypatch):
    """Regression: on an exceptional exit the subprocess must be reaped.

    A fake CLI writes its PID, emits one assistant event, then sleeps 30 s.
    The on_event callback raises on the first event. With no try/finally the
    child would keep sleeping (orphaned); with the fix the finally block kills
    and reaps it.
    """
    import asyncio
    import os
    import time

    pidfile = tmp_path / "cli_pid.txt"
    monkeypatch.setenv("FAKE_CLI_PIDFILE", str(pidfile))

    p = tmp_path / "sleepy_claude.py"
    p.write_text(_FAKE_CLI_SLEEPY.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)

    async def on_event_raises(stream_id, event, usage):
        raise RuntimeError("simulated on_event failure")

    runner = ClaudeCliRunner(on_event_raises, None)

    with pytest.raises(RuntimeError):
        await asyncio.wait_for(
            runner.run(
                binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
                allow_gated=True, allowed_tools=frozenset(), model=None, stream_id="s1",
            ),
            timeout=15,
        )

    assert pidfile.exists(), "fake CLI never wrote its PID — test setup broken"
    pid = int(pidfile.read_text().strip())

    # Poll until the process is reaped (or give up after a few seconds).
    deadline = time.monotonic() + 5.0
    reaped = False
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
            await asyncio.sleep(0.1)
        except (ProcessLookupError, OSError):
            reaped = True
            break

    assert reaped, (
        f"subprocess (pid {pid}) was NOT reaped after ClaudeCliRunner.run raised — "
        "orphaned child is still running"
    )


@pytest.mark.anyio
async def test_runner_drains_stderr_concurrently_no_deadlock(tmp_path):
    """Regression: draining stdout before stderr deadlocks when stderr > pipe buffer.

    The fake CLI writes 2 MB to stderr before its stdout result line. On old
    sequential-drain code the child blocks on the stderr write, the parent waits
    forever on stdout EOF — deadlock. The fix starts an asyncio task to drain stderr
    concurrently so the child never blocks.
    """
    import asyncio

    p = tmp_path / "large_stderr.py"
    p.write_text(_FAKE_CLI_LARGE_STDERR.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)

    runner = ClaudeCliRunner(None, None)
    result = await asyncio.wait_for(
        runner.run(
            binary=str(p), prompt="go", system_prompt="s", cwd=str(tmp_path),
            allow_gated=False, allowed_tools=frozenset(), model=None, stream_id="",
        ),
        timeout=15,
    )
    assert result.output == "ok after big stderr"
