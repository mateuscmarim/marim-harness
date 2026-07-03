"""Unit tests for SpawnTranscripts — the session-bound persistence the runner
delegates a spawn's sidecar transcript and terminal meta to. It reads the store
off the session controller on every call (so it follows a /switch), degrades to
a no-op when there's no store, and stamps the terminal meta template."""

from pathlib import Path
from types import SimpleNamespace

from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    UserPromptPart,
)

from marim_harness.session import SessionStore
from marim_harness.subagents.persistence import SpawnTranscripts, count_tool_calls


def _session(store):
    """A stand-in session controller exposing just the .store the object reads."""
    return SimpleNamespace(store=store)


def _store(tmp_path: Path, sid: str = "s") -> SessionStore:
    return SessionStore(path=tmp_path / "sessions" / f"{sid}.json",
                        workspace_root=tmp_path, session_id=sid, name=sid)


def _msgs() -> list:
    return [
        ModelRequest(parts=[UserPromptPart(content="do it")]),
        ModelResponse(parts=[TextPart(content="done")]),
    ]


def test_save_writes_a_v2_envelope_readable_back(tmp_path: Path):
    t = SpawnTranscripts(_session(_store(tmp_path)), cap=2000)
    meta = {"stream_id": "sg1", "type": "general", "task": "t", "status": "running"}
    t.save("sg1", _msgs(), meta=meta)
    assert t.read_meta("sg1")["type"] == "general"
    assert t.read("sg1") is not None


def test_save_is_a_noop_without_a_store(tmp_path: Path):
    t = SpawnTranscripts(_session(None), cap=2000)
    t.save("sg1", _msgs(), meta={"stream_id": "sg1"})   # must not raise
    assert t.read("sg1") is None
    assert t.read_meta("sg1") is None


def test_read_returns_none_for_a_missing_spawn(tmp_path: Path):
    t = SpawnTranscripts(_session(_store(tmp_path)), cap=2000)
    assert t.read("nope") is None
    assert t.read_meta("nope") is None


def test_has_store_reflects_session_store_presence(tmp_path: Path):
    assert SpawnTranscripts(_session(_store(tmp_path)), cap=2000).has_store is True
    assert SpawnTranscripts(_session(None), cap=2000).has_store is False


def test_persistence_follows_a_session_switch(tmp_path: Path):
    """The object reads the store off the controller each call, so writing after a
    switch lands in the NEW session's dir, not the one it was constructed with."""
    session = _session(_store(tmp_path, "a"))
    t = SpawnTranscripts(session, cap=2000)
    session.store = _store(tmp_path, "b")               # /switch
    t.save("sg1", _msgs(), meta={"stream_id": "sg1", "type": "general"})
    # The sidecar dir sits next to the session file: <session>.parent/<id>.subagents.
    assert (tmp_path / "sessions" / "b.subagents").exists()
    assert not (tmp_path / "sessions" / "a.subagents").exists()


def test_final_meta_stamps_status_usage_tool_count_and_duration(tmp_path: Path):
    t = SpawnTranscripts(_session(_store(tmp_path)), cap=2000)
    template = {"stream_id": "sg1", "type": "general", "status": "running"}
    usage = SimpleNamespace(input_tokens=5, output_tokens=3)
    msgs = [ModelResponse(parts=[
        ToolCallPart(tool_name="read_file", args={"path": "x"}),
        ToolCallPart(tool_name="bash", args={"command": "ls"}),
    ])]
    meta = t.final_meta(template, "finished", usage, t0=0.0, messages=msgs)
    assert meta["status"] == "finished"
    assert meta["usage"] == {"input": 5, "output": 3}
    assert meta["tool_count"] == 2
    assert meta["duration"] >= 0.0
    assert template["status"] == "running"              # template not mutated


def test_final_meta_is_none_when_there_is_no_template(tmp_path: Path):
    t = SpawnTranscripts(_session(_store(tmp_path)), cap=2000)
    assert t.final_meta(None, "finished", None, t0=0.0) is None


def test_count_tool_calls_counts_tool_call_parts():
    msgs = [
        ModelRequest(parts=[UserPromptPart(content="x")]),
        ModelResponse(parts=[ToolCallPart(tool_name="read_file", args={}),
                             TextPart(content="thinking")]),
        ModelResponse(parts=[ToolCallPart(tool_name="bash", args={})]),
    ]
    assert count_tool_calls(msgs) == 2
