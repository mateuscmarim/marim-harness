"""Sidecar checkpointing and interrupted-spawn resume.

A spawn used to write its transcript sidecar only at completion, so a process
death mid-run lost the transcript entirely. The runner now flushes a v2 envelope
(meta + messages) before every model request via a ProcessHistory capability and
finalizes it with a terminal status, so a crashed spawn leaves a resumable trail.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import ModelResponse, TextPart, ToolCallPart
from pydantic_ai.models.function import FunctionModel

from marim_harness.session import SessionStore, TranscriptStore
from tests.conftest import _make_deps, _make_harness


def _session_store(tmp_path: Path) -> SessionStore:
    return SessionStore(
        path=tmp_path / "sessions" / "test.json", workspace_root=tmp_path,
        session_id="test-session", name="test",
    )


def _tool_then_text_model() -> FunctionModel:
    """First request: call list_files. Second: final report."""
    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="list_files", args={"path": "."}, tool_call_id="t1")])
        return ModelResponse(parts=[TextPart(content="report")])
    return FunctionModel(fn)


def _spy_saves(runner):
    """Record every _save_transcript meta status, preserving behavior."""
    seen: list[str | None] = []
    orig = runner._save_transcript

    def spy(stream_id, messages, meta=None):
        seen.append(None if meta is None else meta.get("status"))
        orig(stream_id, messages, meta=meta)

    runner._save_transcript = spy
    return seen


@pytest.mark.anyio
async def test_spawn_checkpoints_running_then_finalizes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_tool_then_text_model(), _make_deps(tmp_path), store=store)
    seen = _spy_saves(harness.subagents)
    out = await harness.subagents.run("general", "look around", stream_id="sg-ck")
    assert out == "report"
    # At least one mid-run checkpoint (per model request) plus the final write.
    assert "running" in seen and seen[-1] == "finished"
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-ck")
    assert meta["status"] == "finished"
    assert meta["type"] == "general" and meta["task"] == "look around"
    assert meta["depth"] == 1 and meta["usage"]["output"] >= 0


@pytest.mark.anyio
async def test_failed_spawn_leaves_sidecar_marked_running(tmp_path):
    """A spawn that dies mid-run gets no final write — its sidecar stays
    status=running, which is exactly what the resume scan treats as interrupted."""
    def fn(messages, info):
        if len(messages) == 1:
            return ModelResponse(parts=[ToolCallPart(
                tool_name="list_files", args={"path": "."}, tool_call_id="t1")])
        raise RuntimeError("boom")  # permanent → no retry, spawn fails

    store = _session_store(tmp_path)
    harness = _make_harness(FunctionModel(fn), _make_deps(tmp_path), store=store)
    out = await harness.subagents.run("general", "task", stream_id="sg-dead")
    assert "failed" in out  # foreground contains the crash as an error string
    meta = TranscriptStore(store.path, store.session_id).read_meta("sg-dead")
    assert meta is not None and meta["status"] == "running"
