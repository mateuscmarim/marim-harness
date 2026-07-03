"""Sidecar checkpointing and interrupted-spawn resume.

A spawn used to write its transcript sidecar only at completion, so a process
death mid-run lost the transcript entirely. The runner now flushes a v2 envelope
(meta + messages) before every model request via a ProcessHistory capability and
finalizes it with a terminal status, so a crashed spawn leaves a resumable trail.
"""

from pathlib import Path

import pytest
from pydantic_ai.messages import (
    ModelRequest,
    ModelResponse,
    TextPart,
    ToolCallPart,
    ToolReturnPart,
    UserPromptPart,
)
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


def _interrupted_meta(sid: str) -> dict:
    return {"stream_id": sid, "type": "general", "task": "original task",
            "model": None, "mcp": None, "depth": 1, "max_output_chars": None,
            "isolation": None, "status": "running"}


def _dangling_history() -> list:
    """A transcript that died mid-tool-call — the resume must repair it."""
    return [
        ModelRequest(parts=[UserPromptPart(content="original task")]),
        ModelResponse(parts=[ToolCallPart(
            tool_name="read_file", args={"path": "x"}, tool_call_id="dangling")]),
    ]


def _resume_model() -> FunctionModel:
    """Asserts the incoming history was repaired (the dangling call has a
    synthesized return), then finishes."""
    def fn(messages, info):
        returns = [p for m in messages for p in getattr(m, "parts", [])
                   if isinstance(p, ToolReturnPart)]
        assert any(p.tool_call_id == "dangling" for p in returns), \
            "resume must synthesize a return for the dangling tool call"
        return ModelResponse(parts=[TextPart(content="resumed-ok")])
    return FunctionModel(fn)


@pytest.mark.anyio
async def test_resume_spawn_repairs_history_and_finishes(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    job_id, message = await harness.subagents.resume_spawn("sg-int")
    assert job_id is not None, message
    report = await harness.deps.jobs.wait(job_id)
    assert report == "resumed-ok"
    job = harness.deps.jobs.get(job_id)
    assert job is not None and job.stream_id == "sg-int"
    assert ts.read_meta("sg-int")["status"] == "finished"


@pytest.mark.anyio
async def test_resume_refuses_v1_finished_and_double_resume(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    # v1 sidecar (no meta) → refuse
    ts.write("sg-v1", _dangling_history(), 2000)
    job_id, msg = await harness.subagents.resume_spawn("sg-v1")
    assert job_id is None and "resumable" in msg.lower()
    # finished spawn → refuse
    ts.write("sg-done", _dangling_history(), 2000,
             meta={**_interrupted_meta("sg-done"), "status": "finished"})
    job_id, msg = await harness.subagents.resume_spawn("sg-done")
    assert job_id is None
    # already resuming → refuse the second call
    ts.write("sg-int", _dangling_history(), 2000, meta=_interrupted_meta("sg-int"))
    first, _ = await harness.subagents.resume_spawn("sg-int")
    assert first is not None
    second, msg = await harness.subagents.resume_spawn("sg-int")
    assert second is None and first in msg
    await harness.deps.jobs.wait(first)


@pytest.mark.anyio
async def test_resume_refuses_when_isolation_branch_is_gone(tmp_path):
    store = _session_store(tmp_path)
    harness = _make_harness(_resume_model(), _make_deps(tmp_path), store=store)
    ts = TranscriptStore(store.path, store.session_id)
    meta = {**_interrupted_meta("sg-iso"), "isolation": "subagent/gone"}
    ts.write("sg-iso", _dangling_history(), 2000, meta=meta)
    job_id, msg = await harness.subagents.resume_spawn("sg-iso")
    assert job_id is None and "subagent/gone" in msg
