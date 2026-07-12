import stat
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from tests.conftest import _make_deps, _make_harness

_FAKE_CLI = '''#!{python}
import json, sys
for o in [
    {{"type": "assistant", "message": {{"content": [{{"type": "text", "text": "hi"}}]}}}},
    {{"type": "result", "subtype": "success", "result": "Done: report body",
      "num_turns": 1, "usage": {{"input_tokens": 7, "output_tokens": 4}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _write_cli_agent(tmp_path: Path) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: CLI worker\nbackend: claude-cli\ntools: read_file\n---\n"
        "You are a CLI worker.\n",
        encoding="utf-8",
    )


def _dummy_model() -> FunctionModel:
    async def fn(messages, info):
        return ModelResponse(parts=[TextPart(content="unused")])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_cli_backend_spawn_returns_report(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), _make_deps(tmp_path)
    ).subagents
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "Done: report body" in out
    assert runner.session.usage.output_tokens == 4


@pytest.mark.anyio
async def test_cli_backend_missing_binary_is_contained(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", "no-such-claude-binary")
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), _make_deps(tmp_path)
    ).subagents
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "failed" in out.lower()  # contained, not raised


@pytest.mark.anyio
async def test_cli_backend_notes_unforwarded_mcp(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(
        _dummy_model(), _make_deps(tmp_path)
    ).subagents
    out = await runner.run("cli-worker", "t", stream_id="s1", mcp_names=["mddocs"])
    assert "mddocs" in out and "not forwarded" in out.lower()


@pytest.mark.anyio
async def test_cli_backend_fires_usage_callback(tmp_path: Path, monkeypatch):
    """After a CLI spawn the on_subagent_usage callback receives the final
    RunUsage so the card and pane can show token counts and cost."""
    from pydantic_ai.usage import RunUsage

    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)

    received: list[tuple[str, RunUsage]] = []

    async def on_usage(stream_id: str, usage) -> None:
        received.append((stream_id, usage))

    deps = _make_deps(tmp_path)
    deps.ui.on_subagent_usage = on_usage
    runner = _make_harness(_dummy_model(), deps).subagents
    await runner.run("cli-worker", "do the thing", stream_id="sg1")

    assert len(received) == 1
    sid, usage = received[0]
    assert sid == "sg1"
    assert isinstance(usage, RunUsage)
    assert usage.input_tokens == 7 and usage.output_tokens == 4


@pytest.mark.anyio
async def test_cli_backend_skips_usage_callback_without_stream_id(
    tmp_path: Path, monkeypatch
):
    """No stream_id means headless / background: no card to update, so the
    callback must not fire."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)

    received: list = []

    async def on_usage(stream_id: str, usage) -> None:
        received.append(usage)

    deps = _make_deps(tmp_path)
    deps.ui.on_subagent_usage = on_usage
    runner = _make_harness(_dummy_model(), deps).subagents
    # Empty stream_id → headless/background: no card to address.
    await runner.run("cli-worker", "do the thing", stream_id="")

    assert received == []


_FAKE_CLI_CHILD = '''#!{python}
import json, sys
for o in [
    {{"type": "assistant", "message": {{"id": "m1", "content": [
        {{"type": "tool_use", "id": "tsub", "name": "Agent",
          "input": {{"description": "d", "subagent_type": "Explore", "prompt": "p"}}}},
    ]}}}},
    {{"type": "system", "subtype": "task_started", "tool_use_id": "tsub"}},
    {{"type": "assistant", "parent_tool_use_id": "tsub",
      "message": {{"id": "m2", "content": [{{"type": "text", "text": "4"}}]}}}},
    {{"type": "system", "subtype": "task_notification", "tool_use_id": "tsub",
      "status": "completed", "summary": "4"}},
    {{"type": "result", "subtype": "success", "result": "Done", "num_turns": 1,
      "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli_child(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude_child.py"
    p.write_text(_FAKE_CLI_CHILD.format(python=sys.executable), encoding="utf-8")
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


@pytest.mark.anyio
async def test_cli_backend_persists_child_transcripts(tmp_path: Path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli_child(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents

    saved: list[str] = []
    real_save = runner._transcripts.save
    monkeypatch.setattr(
        runner._transcripts, "save",
        lambda sid, msgs, meta=None, cap_reasoning=False: (
            saved.append(sid),
            real_save(sid, msgs, meta=meta, cap_reasoning=cap_reasoning),
        ),
    )
    out = await runner.run("cli-worker", "do the thing", stream_id="s1")
    assert "Done" in out
    assert "s1" in saved and "tsub" in saved  # parent sidecar AND the child's


@pytest.mark.anyio
async def test_cli_runner_times_out_on_hung_cli(tmp_path: Path, monkeypatch):
    # A `claude -p` that never EOFs (network hang, an interactive prompt on the
    # inherited stdin) must not pin its concurrency slot forever: the wall-clock
    # timeout SIGKILLs the group and fails the spawn promptly. A fake CLI that just
    # sleeps (emitting no result) can only be ended by the timeout.
    import time as _time

    from marim_harness.subagents.cli_backend import ClaudeCliRunner, CliRunError

    script = tmp_path / "hang.py"
    script.write_text(f"#!{sys.executable}\nimport time\ntime.sleep(30)\n", encoding="utf-8")
    script.chmod(script.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0.3")

    runner = ClaudeCliRunner(None, None)
    start = _time.monotonic()
    with pytest.raises(CliRunError) as exc:
        await runner.run(
            binary=str(script), prompt="p", system_prompt="s", cwd=str(tmp_path),
            allow_gated=False, allowed_tools=[], model=None, stream_id="s1",
        )
    elapsed = _time.monotonic() - start
    assert "timed out" in str(exc.value)
    assert elapsed < 5  # killed at the ~0.3s deadline, not after the 30s sleep


def test_cli_timeout_env_falls_back_on_garbage(monkeypatch):
    # A non-positive / unparseable override must not disable the guard.
    from marim_harness.subagents.cli_backend import _DEFAULT_CLI_TIMEOUT, _cli_timeout

    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "not-a-number")
    assert _cli_timeout() == _DEFAULT_CLI_TIMEOUT
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "0")
    assert _cli_timeout() == _DEFAULT_CLI_TIMEOUT
    monkeypatch.setenv("MARIM_CLAUDE_CLI_TIMEOUT", "42")
    assert _cli_timeout() == 42.0


@pytest.mark.anyio
async def test_cli_backend_schema_appends_prompt_contract(tmp_path: Path, monkeypatch):
    """A claude-cli spawn is an external process marim only launches — it
    can't take a pydantic-ai output type, so the runner (which knows the
    backend) appends the prompt contract to the task instead."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _write_cli_agent(tmp_path)
    runner = _make_harness(_dummy_model(), _make_deps(tmp_path)).subagents
    seen = {}

    async def fake_execute(defn, task, *args, **kwargs):
        seen["task"] = task
        return "ok"

    monkeypatch.setattr(runner._cli, "execute", fake_execute)
    out = await runner.run(
        "cli-worker", "do the thing", "s1",
        output_schema={"type": "object", "properties": {"ok": {"type": "boolean"}}},
    )
    assert out == "ok"
    assert seen["task"].startswith("do the thing")
    assert "Output contract" in seen["task"]
