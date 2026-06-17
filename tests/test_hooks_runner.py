# tests/test_hooks_runner.py
import os
import stat
from pathlib import Path

import pytest

from marim_harness.hooks import events
from marim_harness.hooks.runner import HookRunner, base_payload


def _script(tmp_path: Path, name: str, body: str) -> str:
    p = tmp_path / name
    p.write_text("#!/usr/bin/env bash\n" + body, encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _entry(command: str, *, matcher: str | None = None, timeout: int | None = None) -> dict:
    hook = {"type": "command", "command": command}
    if timeout is not None:
        hook["timeout"] = timeout
    entry: dict = {"hooks": [hook]}
    if matcher is not None:
        entry["matcher"] = matcher
    return entry


def _payload(event: str, **extra) -> dict:
    return base_payload(event, session_id="s1", cwd="/tmp", transcript_path="/tmp/s.json", **extra)


@pytest.mark.anyio
async def test_payload_arrives_on_stdin(tmp_path):
    out = tmp_path / "seen.txt"
    cmd = _script(tmp_path, "h.sh", f"cat > {out}\n")
    runner = HookRunner({events.STOP: [_entry(cmd)]})
    await runner.dispatch(events.STOP, _payload(events.STOP))
    received = out.read_text()
    assert '"hook_event_name": "Stop"' in received
    assert '"session_id": "s1"' in received


@pytest.mark.anyio
async def test_injection_via_hook_specific_output(tmp_path):
    cmd = _script(
        tmp_path, "h.sh",
        'echo \'{"hookSpecificOutput": {"additionalContext": "RECALLED"}}\'\n',
    )
    runner = HookRunner({events.SESSION_START: [_entry(cmd)]})
    ctx = await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START, source="startup"))
    assert ctx == "RECALLED"


@pytest.mark.anyio
async def test_injection_via_plain_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo PLAINTEXT\n")
    runner = HookRunner({events.USER_PROMPT_SUBMIT: [_entry(cmd)]})
    ctx = await runner.dispatch(events.USER_PROMPT_SUBMIT, _payload(events.USER_PROMPT_SUBMIT, prompt="hi"))
    assert ctx == "PLAINTEXT"


@pytest.mark.anyio
async def test_multiple_hooks_concatenate(tmp_path):
    a = _script(tmp_path, "a.sh", "echo AAA\n")
    b = _script(tmp_path, "b.sh", "echo BBB\n")
    runner = HookRunner({events.SESSION_START: [_entry(a), _entry(b)]})
    ctx = await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START, source="startup"))
    assert ctx == "AAA\nBBB"


@pytest.mark.anyio
async def test_observe_event_returns_none_even_with_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo IGNORED\n")
    runner = HookRunner({events.POST_TOOL_USE: [_entry(cmd, matcher="*")]})
    ctx = await runner.dispatch(events.POST_TOOL_USE, _payload(events.POST_TOOL_USE, tool_name="bash"))
    assert ctx is None


@pytest.mark.anyio
async def test_matcher_filters_by_tool_name(tmp_path):
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher="edit_file")]})
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash"))
    assert not out.exists()  # matcher 'edit_file' does not match tool 'bash'
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="edit_file"))
    assert out.read_text().strip() == "ran"


@pytest.mark.anyio
async def test_nonzero_exit_yields_no_context(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo NOPE\nexit 1\n")
    runner = HookRunner({events.SESSION_START: [_entry(cmd)]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_missing_command_is_swallowed(tmp_path):
    runner = HookRunner({events.SESSION_START: [_entry("/no/such/binary/xyzzy")]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_timeout_is_killed_and_swallowed(tmp_path):
    cmd = _script(tmp_path, "h.sh", "sleep 5\necho LATE\n")
    runner = HookRunner({events.SESSION_START: [_entry(cmd, timeout=1)]})
    assert await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START)) is None


@pytest.mark.anyio
async def test_unconfigured_event_returns_none():
    runner = HookRunner({})
    assert await runner.dispatch(events.STOP, _payload(events.STOP)) is None


@pytest.mark.anyio
async def test_invalid_regex_matcher_is_treated_as_no_match(tmp_path):
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher="[")]})
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash"))
    assert not out.exists()  # invalid regex must not crash; treated as no-match


@pytest.mark.anyio
async def test_unknown_hook_type_is_skipped(tmp_path):
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    entry = {"hooks": [{"type": "mystery", "command": cmd}]}
    runner = HookRunner({events.SESSION_START: [entry]})
    ctx = await runner.dispatch(events.SESSION_START, _payload(events.SESSION_START))
    assert ctx is None
    assert not out.exists()  # non-"command" type never executes


@pytest.mark.anyio
async def test_non_string_matcher_is_treated_as_no_match(tmp_path):
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    # Construct entry with a non-string matcher (object) directly
    entry = {"matcher": {"bad": "object"}, "hooks": [{"type": "command", "command": cmd}]}
    runner = HookRunner({events.PRE_TOOL_USE: [entry]})
    # dispatch must not raise; non-string matcher treated as no-match
    ctx = await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash"))
    assert ctx is None
    assert not out.exists()  # non-string matcher must not crash; treated as no-match
