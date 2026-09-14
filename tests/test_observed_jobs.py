"""CLI-owned agent rows share display state without acquiring native control."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest

from marim_harness.jobs import Job, JobRegistry, PrerequisiteFailed, result_tail
from marim_harness.server import http
from marim_harness.server.jobs_view import assemble, detail_dto


def test_observed_lifecycle_notifies_once_and_preserves_identity_and_full_live_result():
    changed = Mock()
    registry = JobRegistry(changed)
    job = registry.observe_agent("cli-child", "investigation", prompt="inspect source")
    assert registry.get(job.id) is job
    assert (job.kind, job.backend_owned, job.task) == ("agent", True, None)
    assert job.started_at and job.finished_at is None
    assert registry.export_settled() == []
    registry.reopen_observed(job)
    assert changed.call_count == 1

    report = "Full report\n" + "details " * 100 + "\nComplete."
    registry.settle_observed(job, "done", report)
    registry.settle_observed(job, "failed", "duplicate event")
    assert job.status == "done"
    assert changed.call_count == 2
    assert registry.output(job.id) == report
    assert detail_dto(job, registry.output(job.id), None)["result"] == report
    assert job.finished_at
    job.started_at = "old-run"
    registry.reopen_observed(job)
    assert changed.call_count == 3
    assert registry.list() == [job]
    assert job.stream_id == "cli-child"
    assert job.started_at != "old-run"
    assert (job.status, job.result, job.finished_at) == ("running", None, None)


def test_observed_rows_round_trip_history_and_list_with_distinct_new_ids():
    registry = JobRegistry()
    job = registry.observe_agent("cli-child", "first", "prompt")
    report = "long " * 100
    registry.settle_observed(job, "failed", report)
    restored = JobRegistry()
    restored.import_history(registry.export_settled())
    [history] = restored.history
    assert history.backend_owned
    assert (history.status, history.result, history.prompt) == (
        "failed",
        result_tail(report),
        "prompt",
    )
    another = restored.observe_agent("cli-child", "second")
    assert another.id != job.id
    rows = assemble(restored.list(), restored.history, lambda _: None)
    assert [(row["id"], row["backend_owned"]) for row in rows] == [
        (another.id, True),
        (job.id, True),
    ]
    # Older histories lack the ownership flag and retain native semantics.
    restored.import_history([{"id": "job-50", "status": "done"}])
    assert not restored.history[0].backend_owned


@pytest.mark.anyio
async def test_observed_agents_neither_wake_nor_block_native_job_completion():
    registry = JobRegistry()
    observed = registry.observe_agent("child", "CLI task")
    assert not registry.any_running()

    async def native_result():
        return "native report"

    native_id = registry.register("agent", "native task", native_result())
    assert registry.any_running()
    native = registry.get(native_id)
    assert native is not None and native.task is not None
    await native.task
    await asyncio.sleep(0)
    assert not registry.any_running()
    assert registry.has_finished_pending()
    digest = registry.take_finished_digest()
    assert "native report" in digest and observed.id not in digest
    registry.settle_observed(observed, "done", "CLI report already delivered")
    assert not registry.has_finished_pending()
    assert registry.take_finished_digest() == ""


@pytest.mark.anyio
async def test_cancellation_and_waiting_cannot_pretend_to_control_an_observed_agent():
    changed = Mock()
    registry = JobRegistry(changed)
    observed = registry.observe_agent("child", "CLI task")
    assert "cannot cancel" in await registry.cancel(observed.id)
    await registry.cancel_all()
    assert observed.status == "running"
    assert changed.call_count == 1
    assert "still running" in await registry.wait(observed.id)
    with pytest.raises(PrerequisiteFailed, match="managed by the CLI backend"):
        await registry.await_settled([observed.id])
    registry.settle_observed(observed, "cancelled", "backend confirmed cancellation")
    assert await registry.wait(observed.id) == "backend confirmed cancellation"
    assert await registry.await_settled([observed.id]) == [observed]
    assert registry.take_finished_digest() == ""


def test_observed_api_refuses_foreign_rows_and_nonterminal_settlement():
    registry = JobRegistry()
    observed = registry.observe_agent("child", "CLI task")
    foreign = Job(observed.id, "agent", "foreign", backend_owned=True)
    with pytest.raises(ValueError, match="this registry"):
        registry.settle_observed(foreign, "done", "result")
    with pytest.raises(ValueError, match="this registry"):
        registry.reopen_observed(foreign)
    with pytest.raises(ValueError, match="terminal"):
        registry.settle_observed(observed, "running", "not finished")
    assert observed.status == "running" and observed.result is None


@pytest.mark.anyio
async def test_discard_observed_preserves_native_work_digest_and_incoming_history():
    changed = Mock()
    registry = JobRegistry(changed)
    release = asyncio.Event()

    async def native_work():
        await release.wait()
        return "native result"

    async def native_done():
        return "native completion"

    running_id = registry.register("agent", "native running", native_work())
    done_id = registry.register("agent", "native done", native_done())
    done = registry.get(done_id)
    assert done is not None and done.task is not None
    await done.task
    await asyncio.sleep(0)
    observed_running = registry.observe_agent("old-running", "old CLI task")
    observed_done = registry.observe_agent("old-done", "finished CLI task")
    registry.settle_observed(observed_done, "done", "old report")
    registry.import_history(
        [{"id": "job-50", "status": "done", "backend_owned": True, "label": "incoming"}]
    )
    incoming_history = registry.history
    changed.reset_mock()
    epoch = registry.observation_epoch

    registry.discard_observed()

    assert registry.observation_epoch == epoch + 1
    assert [job.id for job in registry.list()] == [running_id, done_id]
    assert registry.history is incoming_history
    assert registry.history[0].backend_owned
    assert registry.any_running() and registry.has_finished_pending()
    assert "native completion" in registry.take_finished_digest()
    assert changed.call_count == 1
    assert observed_running.status == "running"  # discarding is not backend cancellation
    for removed in (observed_running, observed_done):
        assert registry.get(removed.id) is None
        with pytest.raises(ValueError, match="this registry"):
            registry.settle_observed(removed, "failed", "late result")
    await registry.cancel(running_id)


def test_discard_without_rows_still_invalidates_late_observers_without_notifying():
    changed = Mock()
    registry = JobRegistry(changed)
    assert registry.observation_epoch == 0
    registry.discard_observed()
    registry.discard_observed()
    assert registry.observation_epoch == 2
    changed.assert_not_called()
    fresh = registry.observe_agent("new-session", "fresh observation")
    assert registry.get(fresh.id) is fresh
    assert registry.observation_epoch == 2


@pytest.mark.anyio
async def test_dependency_cannot_return_an_observer_reopened_while_waiting():
    registry = JobRegistry()
    release = asyncio.Event()

    async def native_result():
        await release.wait()
        return "native report"

    native_id = registry.register("agent", "native task", native_result())
    observed = registry.observe_agent("child", "CLI task")
    registry.settle_observed(observed, "done", "first run")
    waiter = asyncio.create_task(registry.await_settled([native_id, observed.id]))
    await asyncio.sleep(0)
    registry.reopen_observed(observed)
    release.set()
    with pytest.raises(PrerequisiteFailed, match="no registry-owned task"):
        await waiter
    assert observed.status == "running"


@pytest.mark.anyio
async def test_http_cancel_refuses_backend_managed_job_without_mutating(monkeypatch):
    registry = JobRegistry()
    observed = registry.observe_agent("child", "CLI task")
    host = SimpleNamespace(harness=SimpleNamespace(deps=SimpleNamespace(jobs=registry)))
    request = SimpleNamespace(path_params={"job_id": observed.id})
    monkeypatch.setattr(http, "_session_scope", lambda _: (SimpleNamespace(id="ws"), "sid", None))
    monkeypatch.setattr(http, "_supervisor", lambda _: SimpleNamespace(peek=lambda *_: host))
    response = await http.cancel_job(request)
    assert response.status_code == 409
    assert json.loads(response.body)["error"]["code"] == "backend_managed"
    assert observed.status == "running"
