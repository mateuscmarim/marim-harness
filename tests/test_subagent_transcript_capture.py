from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness
from tests.fakes import fake_claude_bin


def _fake_cli(tmp_path: Path) -> str:
    return fake_claude_bin(
        tmp_path,
        {
            "session_id": "sess-abc",
            "model": "claude-test",
            "turns": [
                [
                    {"text": "looking"},
                    {"tool_use": {"id": "c1", "name": "Read", "input": {"file_path": "x"}}},
                    {"tool_result": {"id": "c1", "content": "body"}},
                    {
                        "result": {
                            "result": "done",
                            "usage": {"input_tokens": 1, "output_tokens": 1},
                        }
                    },
                ]
            ],
        },
    )


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
    store = SessionStore(
        path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path, session_id="t", name="t"
    )
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path),
        store=store,
    )
    statuses: list[str | None] = []
    orig = harness.subagents._transcripts.save

    def spy(stream_id, messages, meta=None, cap_reasoning=False):
        statuses.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta, cap_reasoning=cap_reasoning)

    harness.subagents._transcripts.save = spy
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
    dead = fake_claude_bin(
        tmp_path,
        {
            "session_id": "sess-dead",
            "turns": [[{"text": "partial"}, {"exit": {"code": 1}}]],
        },
    )
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", dead)
    _cli_agent(tmp_path)
    store = SessionStore(
        path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path, session_id="t", name="t"
    )
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path),
        store=store,
    )
    out = await harness.subagents.run("cli-worker", "do it", stream_id="sg-dead")
    assert "failed" in out  # foreground containment
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None
    assert meta["status"] == "running" and meta["cli_session_id"] == "sess-dead"


@pytest.mark.anyio
async def test_cli_final_meta_records_tool_count_and_duration(tmp_path, monkeypatch):
    """CLI spawns stamp the same stats keys the native terminal meta carries
    (tool_count/duration), so their cards rehydrate identically on resume."""
    monkeypatch.setenv("MARIM_CLAUDE_CLI_BIN", _fake_cli(tmp_path))
    _cli_agent(tmp_path)
    store = SessionStore(
        path=tmp_path / "sessions" / "t.json", workspace_root=tmp_path, session_id="t", name="t"
    )
    harness = _make_harness(
        FunctionModel(lambda m, i: ModelResponse(parts=[TextPart(content="x")])),
        _make_deps(tmp_path),
        store=store,
    )
    await harness.subagents.run("cli-worker", "do it", stream_id="sg-cli-stats")
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-cli-stats")
    assert meta["tool_count"] == 1  # the fake CLI's single Read call
    assert meta["duration"] > 0
