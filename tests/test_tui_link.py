"""The in-process session link (phase 4a): ``LocalSessionLink`` exposes the
host and harness behind the same surface the remote link has, and its
``info`` is a live view over the harness (no snapshot to go stale)."""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic_ai.models.test import TestModel

from marim_harness.interfaces.tui.link import LocalSessionLink
from marim_harness.runtime.deps import Deps, UIHooks, WorkspaceConfig
from marim_harness.runtime.harness import Harness
from marim_harness.runtime.permissions import Mode
from marim_harness.server.bus import EventBus
from marim_harness.server.host import SessionHost
from marim_harness.tools.provider import BuiltinToolProvider


def _harness(root: Path) -> Harness:
    deps = Deps(workspace=WorkspaceConfig(root=root, mode=Mode.ask), ui=UIHooks())
    return Harness(TestModel(call_tools=[]), BuiltinToolProvider(), deps, instructions="test")


@pytest.mark.anyio
async def test_local_link_reports_the_host_status_and_mirrors_the_harness(tmp_path):
    harness = _harness(tmp_path)
    host = SessionHost(harness, EventBus(), autonomous_wake=False)
    link = LocalSessionLink(harness, host)
    try:
        assert link.kind == "local"
        assert await link.load_session() == host.status == "idle"
        info = link.info
        assert info.workspace_root == tmp_path
        assert info.mode == "ask" and info.model_label == harness.model_label
        assert info.session_id is None  # no store: an anonymous session
        await link.set_mode("plan")
        assert harness.mode is Mode.plan and info.mode == "plan"  # live, not a snapshot
        assert await link.pending_asks() == []
        assert await link.interrupt() is False  # nothing running
    finally:
        await link.close()  # stops the host, as on_unmount does


@pytest.mark.anyio
async def test_local_link_jobs_surface_is_the_registry(tmp_path):
    """Phase 4b: the jobs read is history + live rows (what the panel paints),
    the actions are the registry's own (same wording as the in-process
    commands), and a resume goes through the harness's runner seam."""
    harness = _harness(tmp_path)
    host = SessionHost(harness, EventBus(), autonomous_wake=False)
    link = LocalSessionLink(harness, host)
    try:
        registry = harness.deps.jobs
        registry.import_history(
            [{"id": "job-1", "kind": "agent", "label": "l", "status": "done", "result_tail": "r"}]
        )

        async def _work() -> str:
            return "done"

        job_id = registry.register("bash", "x", _work())
        await registry.wait(job_id)
        assert [j.id for j in await link.jobs()] == ["job-1", job_id]
        assert await link.job_output(job_id) == "done"
        assert await link.job_output("job-9") == "No job 'job-9'."
        assert await link.cancel_job(job_id) == f"job {job_id} already done"
        resumed, message = await link.resume_spawn("sg-never")
        assert resumed is None and message  # refused with the runner's reason
        harness.deps.services.resume_subagent = None
        assert await link.resume_spawn("sg-never") == (
            None,
            "sub-agent resume is not available in this session",
        )
    finally:
        await link.close()
