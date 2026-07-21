# tests/test_hooks_runner.py
import stat
from pathlib import Path

import pytest

from marim_harness.hooks import events
from marim_harness.hooks.runner import HookRunner, HookVerdict, base_payload


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
    ctx = await runner.dispatch(
        events.SESSION_START, _payload(events.SESSION_START, source="startup")
    )
    assert ctx == "RECALLED"


@pytest.mark.anyio
async def test_injection_via_plain_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo PLAINTEXT\n")
    runner = HookRunner({events.USER_PROMPT_SUBMIT: [_entry(cmd)]})
    ctx = await runner.dispatch(
        events.USER_PROMPT_SUBMIT, _payload(events.USER_PROMPT_SUBMIT, prompt="hi")
    )
    assert ctx == "PLAINTEXT"


@pytest.mark.anyio
async def test_multiple_hooks_concatenate(tmp_path):
    a = _script(tmp_path, "a.sh", "echo AAA\n")
    b = _script(tmp_path, "b.sh", "echo BBB\n")
    runner = HookRunner({events.SESSION_START: [_entry(a), _entry(b)]})
    ctx = await runner.dispatch(
        events.SESSION_START, _payload(events.SESSION_START, source="startup")
    )
    assert ctx == "AAA\nBBB"


@pytest.mark.anyio
async def test_observe_event_returns_none_even_with_stdout(tmp_path):
    cmd = _script(tmp_path, "h.sh", "echo IGNORED\n")
    runner = HookRunner({events.POST_TOOL_USE: [_entry(cmd, matcher="*")]})
    ctx = await runner.dispatch(
        events.POST_TOOL_USE, _payload(events.POST_TOOL_USE, tool_name="bash")
    )
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
async def test_matcher_is_anchored_not_substring(tmp_path):
    """Claude Code matcher semantics are a full/anchored regex match, not a
    substring search: 'Edit' must fire for 'Edit' but NOT for 'MultiEdit'."""
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher="Edit")]})
    # Over-match guard: a substring search would fire 'Edit' for 'MultiEdit'.
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="MultiEdit"))
    assert not out.exists()  # 'Edit' must NOT match 'MultiEdit'
    # Exact match still fires.
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="Edit"))
    assert out.read_text().strip() == "ran"


@pytest.mark.anyio
async def test_wildcard_and_empty_matcher_still_match_everything(tmp_path):
    """The 'all' sentinels ('*', empty, absent) must keep matching any tool
    name even under anchored matching."""
    for i, matcher in enumerate(("*", "", None)):
        out = tmp_path / f"ran{i}.txt"
        cmd = _script(tmp_path, f"h{i}.sh", f"echo ran >> {out}\n")
        runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher=matcher)]})
        await runner.dispatch(
            events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="MultiEdit")
        )
        assert out.read_text().strip() == "ran", f"matcher {matcher!r} should match all"


@pytest.mark.anyio
async def test_matcher_regex_alternation_is_anchored(tmp_path):
    """A regex matcher still works, but as a full match: '(Edit|Write)' matches
    'Edit'/'Write' exactly and not 'MultiEdit'."""
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo {{}} >> {out}\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, matcher="Edit|Write")]})
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="MultiEdit"))
    assert not out.exists()  # anchored alternation must not substring-match
    await runner.dispatch(events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="Write"))
    assert out.exists()


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
async def test_invalid_timeout_falls_back_to_default(tmp_path):
    """A non-numeric timeout from config must not raise TypeError into wait_for
    (which would drop the hook and leak its already-spawned subprocess) — it
    falls back to the default and the hook still runs."""
    cmd = _script(tmp_path, "h.sh", "echo OK\n")
    entry = {"hooks": [{"type": "command", "command": cmd, "timeout": "abc"}]}
    runner = HookRunner({events.USER_PROMPT_SUBMIT: [entry]})
    ctx = await runner.dispatch(
        events.USER_PROMPT_SUBMIT, _payload(events.USER_PROMPT_SUBMIT, prompt="hi")
    )
    assert ctx == "OK"


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
    ctx = await runner.dispatch(
        events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash")
    )
    assert ctx is None


@pytest.mark.anyio
async def test_missing_command_failure_is_logged_at_debug(caplog, tmp_path):
    """A hook command that doesn't exist (ENOENT) is swallowed — but the
    failure must be visible at DEBUG so an operator can diagnose misconfig."""
    import logging
    runner = HookRunner({events.PRE_TOOL_USE: [_entry("/no/such/binary/xyzzy")]})
    with caplog.at_level(logging.DEBUG, logger="marim_harness.hooks.runner"):
        result = await runner.dispatch(
            events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash")
        )
    assert result is None
    assert any(
        "/no/such/binary" in r.message or "spawn" in r.message.lower()
        for r in caplog.records
    ), f"no debug log about missing hook command: {[r.message for r in caplog.records]}"


@pytest.mark.anyio
async def test_timeout_failure_is_logged_at_debug(caplog, tmp_path):
    """A hook that exceeds its deadline is swallowed — log it at DEBUG so an
    operator can spot a misbehaving hook."""
    import logging
    cmd = _script(tmp_path, "h.sh", "sleep 5\necho LATE\n")
    runner = HookRunner({events.PRE_TOOL_USE: [_entry(cmd, timeout=1)]})
    with caplog.at_level(logging.DEBUG, logger="marim_harness.hooks.runner"):
        result = await runner.dispatch(
            events.PRE_TOOL_USE, _payload(events.PRE_TOOL_USE, tool_name="bash")
        )
    assert result is None
    # The exact message wording may vary; just confirm some debug-level record
    # was emitted by the runner module during this dispatch.
    assert any(
        r.name == "marim_harness.hooks.runner" and r.levelno == logging.DEBUG
        for r in caplog.records
    ), f"no DEBUG record from runner: {[(r.name, r.levelname) for r in caplog.records]}"


@pytest.mark.anyio
async def test_verdict_exit_2_blocks_with_stderr_reason(tmp_path):
    cmd = _script(tmp_path, "block.sh", 'echo "dirty git state" >&2\nexit 2\n')
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd)]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert isinstance(v, HookVerdict)
    assert v.blocked and "dirty git state" in v.reason


@pytest.mark.anyio
async def test_verdict_json_decision_block(tmp_path):
    cmd = _script(tmp_path, "jb.sh", """echo '{"decision": "block", "reason": "nope"}'\n""")
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd)]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert v.blocked and v.reason == "nope"


@pytest.mark.anyio
async def test_verdict_clean_exit_and_malformed_json_do_not_block(tmp_path):
    bodies = ("exit 0\n", "echo not-json\n", 'echo \'{"decision": "allow"}\'\n')
    for i, body in enumerate(bodies):
        cmd = _script(tmp_path, f"ok{i}.sh", body)
        runner = HookRunner({events.PRE_COMPACT: [_entry(cmd)]})
        v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "auto"})
        assert not v.blocked


@pytest.mark.anyio
async def test_verdict_crash_and_other_exit_codes_are_not_blocks(tmp_path):
    cmd = _script(tmp_path, "crash.sh", "exit 1\n")
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd)]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert not v.blocked
    missing = HookRunner({events.PRE_COMPACT: [_entry("/nonexistent/hook")]})
    assert not (await missing.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})).blocked


@pytest.mark.anyio
async def test_verdict_timeout_is_not_a_block(tmp_path):
    """A verdict hook that sleeps past its timeout is killed and yields a
    non-blocking verdict — a timeout must never be read as a deliberate block,
    or a slow/hung PreCompact hook could wedge a manual /compact. Mirrors
    test_timeout_is_killed_and_swallowed for the verdict path."""
    cmd = _script(tmp_path, "slow.sh", "sleep 5\nexit 2\n")  # would block if it ran
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd, timeout=1)]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert isinstance(v, HookVerdict)
    assert not v.blocked


@pytest.mark.anyio
async def test_verdict_matcher_matches_trigger(tmp_path):
    cmd = _script(tmp_path, "m.sh", "exit 2\n")
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd, matcher="manual")]})
    assert (await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})).blocked
    assert not (await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "auto"})).blocked


@pytest.mark.anyio
async def test_verdict_unconfigured_event_allows():
    assert not (await HookRunner({}).dispatch_verdict(events.PRE_COMPACT, {})).blocked


@pytest.mark.anyio
async def test_dispatch_precompact_matcher_matches_trigger(tmp_path):
    """The observe-only dispatch() path must gate PreCompact hooks on the
    payload's ``trigger`` (compact events ride the tool_name matcher slot with
    their trigger), the same as dispatch_verdict already does — a plain
    dispatch() call must not silently skip a matchered PreCompact hook."""
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.PRE_COMPACT: [_entry(cmd, matcher="manual")]})
    await runner.dispatch(events.PRE_COMPACT, {"trigger": "auto"})
    assert not out.exists()  # matcher 'manual' does not match trigger 'auto'
    await runner.dispatch(events.PRE_COMPACT, {"trigger": "manual"})
    assert out.read_text().strip() == "ran"


@pytest.mark.anyio
async def test_dispatch_postcompact_matcher_matches_trigger(tmp_path):
    """PostCompact must be gated the same way: matcher filtering is inert
    unless POST_COMPACT is in _matches's event-gating tuple."""
    out = tmp_path / "ran.txt"
    cmd = _script(tmp_path, "h.sh", f"echo ran >> {out}\n")
    runner = HookRunner({events.POST_COMPACT: [_entry(cmd, matcher="manual")]})
    await runner.dispatch(events.POST_COMPACT, {"trigger": "auto"})
    assert not out.exists()  # matcher 'manual' does not match trigger 'auto'
    await runner.dispatch(events.POST_COMPACT, {"trigger": "manual"})
    assert out.read_text().strip() == "ran"


@pytest.mark.anyio
async def test_verdict_first_block_wins_reason(tmp_path):
    """Two matching PreCompact entries both block; the returned verdict must
    carry the FIRST blocking reason, even though later hooks still execute."""
    first = _script(tmp_path, "first.sh", 'echo "reason A" >&2\nexit 2\n')
    second = _script(tmp_path, "second.sh", 'echo "reason B" >&2\nexit 2\n')
    runner = HookRunner({events.PRE_COMPACT: [_entry(first), _entry(second)]})
    v = await runner.dispatch_verdict(events.PRE_COMPACT, {"trigger": "manual"})
    assert v.blocked
    assert "reason A" in v.reason
    assert "reason B" not in v.reason
