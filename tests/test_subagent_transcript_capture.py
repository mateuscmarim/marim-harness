import stat
import sys
from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness

_FAKE_CLI = '''#!{python}
import json, sys
for o in [
    {{"type": "system", "subtype": "init", "session_id": "sess-abc",
      "model": "claude-test"}},
    {{"type": "assistant", "message": {{"content": [
        {{"type": "text", "text": "looking"}},
        {{"type": "tool_use", "id": "c1", "name": "Read", "input": {{"file_path": "x"}}}},
    ]}}}},
    {{"type": "user", "message": {{"content": [
        {{"type": "tool_result", "tool_use_id": "c1", "content": "body"}},
    ]}}}},
    {{"type": "result", "subtype": "success", "result": "done",
      "num_turns": 1, "usage": {{"input_tokens": 1, "output_tokens": 1}}}},
]:
    sys.stdout.write(json.dumps(o) + "\\n")
'''


def _fake_cli(tmp_path: Path) -> str:
    p = tmp_path / "fake_claude.py"
    p.write_text(_FAKE_CLI.format(python=sys.executable))
    p.chmod(p.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    return str(p)


def _cli_agent(tmp_path: Path) -> None:
    d = tmp_path / ".marim" / "agents"
    d.mkdir(parents=True, exist_ok=True)
    (d / "cli-worker.md").write_text(
        "---\ndescription: w\nbackend: claude-cli\ntools: read_file\n---\nWork.\n"
    )


@pytest.mark.anyio
async def test_cli_spawn_writes_transcript_sidecar(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _cli_agent(tmp_path)
    session_store = SessionStore(
        path=tmp_path / "sessions" / "test.json",
        workspace_root=tmp_path,
        session_id="test-session",
        name="test",
    )
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path),
        store=session_store,
    )
    await harness.subagents.run("cli-worker", "do it", stream_id="sg1")
    ts = TranscriptStore(harness.session.store.path, harness.session.store.session_id)
    saved = ts.read("sg1")
    assert saved is not None and len(saved) >= 2  # assistant + tool-return messages


@pytest.mark.anyio
async def test_cli_spawn_checkpoints_with_backend_meta(tmp_path, monkeypatch):
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = SessionStore(path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path,
                         session_id="t", name="t")
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path), store=store,
    )
    statuses: list[str | None] = []
    orig = harness.subagents._save_transcript

    def spy(stream_id, messages, meta=None, cap_reasoning=False):
        statuses.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta, cap_reasoning=cap_reasoning)

    harness.subagents._save_transcript = spy
    await harness.subagents.run("cli-worker", "do it", stream_id="sg-cli")
    # Mid-run checkpoints say "running"; the parent's completion write is last
    # ("finished" — this fake spawns no Claude-side children, so no trailing
    # meta-less child write follows it).
    assert "running" in statuses and statuses[-1] == "finished"
    ts = TranscriptStore(store.path, store.session_id)
    meta = ts.read_meta("sg-cli")
    assert meta["backend"] == "claude-cli"
    assert meta["cli_session_id"] == "sess-abc"
    assert meta["status"] == "finished"


@pytest.mark.anyio
async def test_killed_cli_spawn_rests_at_running_with_session_id(tmp_path, monkeypatch):
    """A CLI process that dies without a result leaves the checkpointed sidecar
    at status=running with the session id — the resumable trail."""
    dead = tmp_path / "dead_claude.py"
    dead.write_text(
        f"#!{sys.executable}\n"
        "import json, sys\n"
        'sys.stdout.write(json.dumps({"type": "system", "subtype": "init",'
        ' "session_id": "sess-dead", "model": "m"}) + "\\n")\n'
        'sys.stdout.write(json.dumps({"type": "assistant", "message": {"content":'
        ' [{"type": "text", "text": "partial"}]}}) + "\\n")\n'
        "sys.exit(1)\n"
    )
    dead.chmod(dead.stat().st_mode | stat.S_IEXEC | stat.S_IRWXU)
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", str(dead))
    _cli_agent(tmp_path)
    store = SessionStore(path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path,
                         session_id="t", name="t")
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path), store=store,
    )
    out = await harness.subagents.run("cli-worker", "do it", stream_id="sg-dead")
    assert "failed" in out  # foreground containment
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None
    assert meta["status"] == "running" and meta["cli_session_id"] == "sess-dead"
