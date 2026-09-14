"""Job completion writes cannot block transport or replace saved conversations."""

import asyncio
import threading

import pytest
from pydantic_ai.messages import ModelRequest, ToolCallPart, UserPromptPart

from marim_harness.session import SessionManager
from tests.conftest import _make_deps, _make_harness, _text_model


def make_harness(tmp_path):
    manager = SessionManager(tmp_path)
    store = manager.create("original")
    harness = _make_harness(_text_model(), _make_deps(tmp_path), store=store, manager=manager)
    harness.session.history = [ModelRequest(parts=[UserPromptPart("saved")])]
    harness.session.persist()
    job = harness.deps.jobs.observe_agent("child", "work")
    harness.deps.jobs.settle_observed(job, "done", "report")
    return harness, store, job


def test_jobs_patch_preserves_disk_history_when_approval_starts_after_queue(tmp_path):
    harness, store, job = make_harness(tmp_path)
    saved_history = store.load()[0]
    write = harness.session.jobs_writer(store, harness.deps.jobs.observation_epoch)
    assert write is not None
    harness.deps.approval_round_active = True
    harness.session.history.append(ModelRequest(parts=[ToolCallPart("bash", {}, "dirty")]))
    write()
    assert store.load()[0] == saved_history
    assert store.load()[4][0]["id"] == job.id


def test_queued_jobs_patch_ignores_session_switch_and_clear(tmp_path):
    harness, store, _ = make_harness(tmp_path)
    write = harness.session.jobs_writer(store, harness.deps.jobs.observation_epoch)
    harness.new_session("incoming")
    harness.session.persist()
    incoming = harness.session.store.path.read_bytes()
    write()
    assert harness.session.store.path.read_bytes() == incoming
    assert store.load()[4] == []
    # A missing baseline is never recreated by a delayed metadata write.
    harness.session.store = store
    write = harness.session.jobs_writer(store, harness.deps.jobs.observation_epoch)
    store.clear()
    write()
    assert not store.path.exists()


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_phase", ["mcp", "cli"])
async def test_cancelled_shutdown_closes_transport_before_draining_and_releasing(
    tmp_path, monkeypatch, cancel_phase
):
    from tests.test_external_cli_model import _Fake

    harness, store, job = make_harness(tmp_path)
    harness.deps.jobs.reopen_observed(job)
    started = asyncio.Event()
    release = asyncio.Event()
    released = []

    class ClosingModel(_Fake):
        async def aclose(self):
            if cancel_phase == "cli":
                started.set()
            await release.wait()
            harness.deps.jobs.settle_observed(job, "failed", "transport closed")
            harness._persist_cli_jobs(store)

    async def close_mcp():
        if cancel_phase == "mcp":
            started.set()
            await asyncio.Event().wait()

    harness.current_model = ClosingModel()
    monkeypatch.setattr(harness.mcp, "aclose", close_mcp)
    monkeypatch.setattr(harness, "release_claim", lambda: released.append(True))
    close = asyncio.create_task(harness.aclose())
    try:
        await asyncio.wait_for(started.wait(), 2)
        close.cancel()
        await asyncio.sleep(0)
        assert released == []
    finally:
        release.set()
        with pytest.raises(asyncio.CancelledError):
            await asyncio.wait_for(close, 5)
    assert released == [True]
    assert store.load()[4][0]["status"] == "failed"


@pytest.mark.parametrize("newer_save_succeeds", [True, False])
def test_jobs_patch_ordered_against_full_persist(tmp_path, monkeypatch, newer_save_succeeds):
    harness, store, job = make_harness(tmp_path)
    write = harness.session.jobs_writer(store, harness.deps.jobs.observation_epoch)
    original = store.save
    if newer_save_succeeds:
        harness.deps.jobs.reopen_observed(job)
        harness.session.persist(force=True)
    else:

        def fail(*args, **kwargs):
            raise OSError("disk failed")

        monkeypatch.setattr(store, "save", fail)
        with pytest.raises(OSError):
            harness.session.persist(force=True)
        monkeypatch.setattr(store, "save", original)
    write()
    assert bool(store.load()[4]) is not newer_save_succeeds


@pytest.mark.anyio
@pytest.mark.parametrize("cancel_close", [False, True])
async def test_blocked_jobs_write_leaves_transport_loop_responsive_and_close_drains(
    tmp_path, monkeypatch, cancel_close
):
    harness, store, _ = make_harness(tmp_path)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    loop_thread = threading.get_ident()
    original = store.save_jobs
    released = []
    monkeypatch.setattr(harness, "release_claim", lambda: released.append(True))

    def blocked(jobs):
        assert threading.get_ident() != loop_thread
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(jobs)

    monkeypatch.setattr(store, "save_jobs", blocked)
    harness._persist_cli_jobs(store)
    close = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        close = asyncio.create_task(harness.aclose())
        await asyncio.sleep(0)
        assert not close.done()
        if cancel_close:
            close.cancel()
            await asyncio.sleep(0)
            assert not close.done()
        assert released == []
        assert store.load()[4] == []
    finally:
        release.set()
        if close is not None:
            if cancel_close:
                with pytest.raises(asyncio.CancelledError):
                    await asyncio.wait_for(close, 5)
            else:
                await asyncio.wait_for(close, 5)
    assert released == [True]
    assert store.load()[4][0]["status"] == "done"


@pytest.mark.anyio
async def test_pending_snapshots_coalesce_and_persistence_recovers_after_failure(
    tmp_path, monkeypatch, caplog
):
    harness, store, _ = make_harness(tmp_path)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store.save_jobs
    writes = []

    def first_fails(jobs):
        writes.append(jobs)
        if len(writes) == 1:
            loop.call_soon_threadsafe(started.set)
            assert release.wait(5)
            raise OSError("simulated write failure")
        return original(jobs)

    monkeypatch.setattr(store, "save_jobs", first_fails)
    harness._persist_cli_jobs(store)
    try:
        await asyncio.wait_for(started.wait(), 2)
        for number in range(3):
            job = harness.deps.jobs.observe_agent(str(number), "more work")
            harness.deps.jobs.settle_observed(job, "done", "report")
            harness._persist_cli_jobs(store)
    finally:
        release.set()
        await harness.cli_job_persistence.flush()
    assert [len(jobs) for jobs in writes] == [1, 4]
    assert len(store.load()[4]) == 4
    assert "CLI job history persist failed" in caplog.text
    assert "simulated write failure" in caplog.text


@pytest.mark.anyio
async def test_completion_during_older_full_write_is_not_superseded(tmp_path, monkeypatch):
    harness, store, job = make_harness(tmp_path)
    harness.deps.jobs.reopen_observed(job)
    started = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store.save

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(*args, **kwargs)

    monkeypatch.setattr(store, "save", blocked)
    persist = asyncio.create_task(asyncio.to_thread(harness.session.persist, force=True))
    try:
        await asyncio.wait_for(started.wait(), 2)
        harness.deps.jobs.settle_observed(job, "done", "new completion")
        harness._persist_cli_jobs(store)
    finally:
        release.set()
        await persist
        await harness.cli_job_persistence.flush()
    assert store.load()[4][0]["result_tail"] == "new completion"


@pytest.mark.anyio
async def test_clear_cannot_be_undone_by_an_inflight_jobs_patch(tmp_path, monkeypatch):
    from marim_harness.session import store as store_module

    harness, store, _ = make_harness(tmp_path)
    write = harness.session.jobs_writer(store, harness.deps.jobs.observation_epoch)
    started = asyncio.Event()
    clearing = asyncio.Event()
    release = threading.Event()
    loop = asyncio.get_running_loop()
    original = store_module.atomic_write_text

    def blocked(*args, **kwargs):
        loop.call_soon_threadsafe(started.set)
        assert release.wait(5)
        return original(*args, **kwargs)

    def clear():
        loop.call_soon_threadsafe(clearing.set)
        store.clear()

    monkeypatch.setattr(store_module, "atomic_write_text", blocked)
    writer = asyncio.create_task(asyncio.to_thread(write))
    clearer = None
    try:
        await asyncio.wait_for(started.wait(), 2)
        clearer = asyncio.create_task(asyncio.to_thread(clear))
        await asyncio.wait_for(clearing.wait(), 2)
        # Let clear attempt to acquire the file lock while the patch is paused
        # after reading the baseline but before its atomic replacement.
        await asyncio.sleep(0.05)
    finally:
        release.set()
        await writer
        if clearer is not None:
            await clearer
    assert not store.path.exists()
