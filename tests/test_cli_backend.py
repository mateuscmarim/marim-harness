import json
import stat
import sys
from pathlib import Path

import pytest

from marim_harness.subagents.cli_backend import ClaudeCliRunner, CliRunError, build_cli_argv


def test_build_cli_argv_resume_and_no_system():
    argv = build_cli_argv(
        "claude",
        "do the thing",
        "SYSTEM",
        "acceptEdits",
        [],
        None,
        resume_session_id="sess-123",
        append_system=False,
    )
    assert "--resume" in argv
    assert argv[argv.index("--resume") + 1] == "sess-123"
    assert "--append-system-prompt" not in argv


def test_build_cli_argv_defaults_unchanged():
    # Existing sub-agent call shape must still include the system prompt and no resume.
    argv = build_cli_argv("claude", "task", "SYSTEM", "plan", ["Read"], "sonnet")
    assert "--append-system-prompt" in argv
    assert "--resume" not in argv
    assert argv[argv.index("--model") + 1] == "sonnet"
    # Safe mode is opt-in; default callers (sub-agents) keep their full environment.
    assert "--safe-mode" not in argv


def test_build_cli_argv_disallowed_tools():
    # --allowedTools is ADDITIVE pre-approval only: under a permissive
    # --permission-mode (Claude Code's plan mode auto-allows WebSearch/WebFetch)
    # absence from the allowlist denies nothing. --disallowedTools is the hard
    # deny headless -p honors — and it must be emitted even when the allowlist
    # maps empty and --allowedTools is omitted entirely.
    argv = build_cli_argv(
        "claude", "task", "SYSTEM", "plan", [], None,
        disallowed_tools=["WebFetch", "WebSearch"],
    )
    assert "--allowedTools" not in argv
    assert argv[argv.index("--disallowedTools") + 1] == "WebFetch,WebSearch"


def test_build_cli_argv_no_disallowed_by_default():
    # Callers that don't deny anything (auto/ask spawns) must not grow a
    # spurious --disallowedTools flag.
    argv = build_cli_argv("claude", "task", "SYSTEM", "acceptEdits", ["Read"], None)
    assert "--disallowedTools" not in argv


def test_build_cli_argv_safe_mode():
    # The main-loop model runs claude in safe mode so the user's plugins/hooks
    # (e.g. agentmemory's cross-session context injection) don't pollute the turn.
    argv = build_cli_argv("claude", "task", "SYSTEM", "plan", [], None, safe_mode=True)
    assert "--safe-mode" in argv


_FAKE_STREAM = '''#!{python}
import json, sys
for o in [
    {{"type": "system", "subtype": "init", "session_id": "sess-abc",
      "model": "claude-test"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "step one"}}]}}}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "step two"}}]}}}},
    {{"type": "result", "subtype": "success", "result": "done", "num_turns": 1,
      "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _script(tmp_path: Path, body: str) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(body.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_run_captures_session_id_and_checkpoints(tmp_path):
    seen: list[tuple[int, str | None]] = []

    def ckpt(messages: list, session_id: str | None) -> None:
        seen.append((len(messages), session_id))

    runner = ClaudeCliRunner(None, None)
    result = await runner.run(
        binary=_script(tmp_path, _FAKE_STREAM), prompt="task", system_prompt="sys",
        cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
        stream_id="sg-cli", checkpoint=ckpt,
    )
    assert result.session_id == "sess-abc"
    # The transcript grew twice (two assistant messages); each growth checkpointed,
    # and every checkpoint after the init line carries the captured session id.
    assert [n for n, _ in seen] == [1, 2]
    assert all(sid == "sess-abc" for _, sid in seen)


@pytest.mark.anyio
async def test_resume_session_id_threads_into_argv(tmp_path):
    argv_file = tmp_path / "argv.json"
    body = (
        "#!{python}\n"
        "import json, sys\n"
        f"open({str(argv_file)!r}, 'w').write(json.dumps(sys.argv))\n"
        'sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",'
        ' "result": "ok", "num_turns": 1, "usage": {{}}}}) + "\\n")\n'
    )
    runner = ClaudeCliRunner(None, None)
    await runner.run(
        binary=_script(tmp_path, body), prompt="continue", system_prompt="sys",
        cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
        stream_id="sg-cli", resume_session_id="sess-abc",
    )
    argv = json.loads(argv_file.read_text())
    assert "--resume" in argv and argv[argv.index("--resume") + 1] == "sess-abc"
    assert "--append-system-prompt" not in argv  # session already has its prompt


_STREAM_WITH_NOISE = '''#!{python}
import sys
sys.stdout.write("not json at all\\n")
sys.stdout.write("{{\\n")  # a line that looks JSON-ish but fails to parse
import json
sys.stdout.write(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "hi"}}]}}}}) + "\\n")
sys.stdout.write(json.dumps({{"type": "result", "subtype": "success",
    "result": "done despite noise", "num_turns": 1, "usage": {{}}}}) + "\\n")
'''


@pytest.mark.anyio
async def test_run_skips_non_json_lines(tmp_path):
    # Characterization test (pinning current behavior before extracting
    # _process_line): a line that fails json.loads is silently skipped —
    # it must not crash the run or appear in the transcript/result.
    runner = ClaudeCliRunner(None, None)
    result = await runner.run(
        binary=_script(tmp_path, _STREAM_WITH_NOISE), prompt="task", system_prompt="sys",
        cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
        stream_id="sg-cli",
    )
    assert result.output == "done despite noise"


_STREAM_NO_RESULT = '''#!{python}
import json, sys
sys.stdout.write(json.dumps({{"type": "assistant", "message": {{"content": [
    {{"type": "text", "text": "no result ever comes"}}]}}}}) + "\\n")
sys.stderr.write("some diagnostic noise\\n")
'''


@pytest.mark.anyio
async def test_run_raises_when_stream_ends_without_result(tmp_path):
    # Characterization test (pinning current behavior before extracting
    # _finalize): a process that exits cleanly (EOF, not a timeout) without
    # ever emitting a "result" event raises CliRunError, with the failure
    # detail sourced from stderr.
    runner = ClaudeCliRunner(None, None)
    with pytest.raises(CliRunError) as exc:
        await runner.run(
            binary=_script(tmp_path, _STREAM_NO_RESULT), prompt="task", system_prompt="sys",
            cwd=str(tmp_path), allow_gated=False, allowed_tools=[], model=None,
            stream_id="sg-cli",
        )
    assert "no result" in str(exc.value)
    assert "some diagnostic noise" in str(exc.value)
