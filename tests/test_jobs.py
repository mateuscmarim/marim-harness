import asyncio

import pytest

from marim_harness.jobs import Job, JobRegistry, render_jobs


async def _settled(reg: JobRegistry, *, tries: int = 400) -> None:
    """Poll until every job has left the 'running' state, yielding to the loop so
    the background tasks settle — without consuming results via wait() (the digest
    must still observe them). Replaces fixed ``asyncio.sleep`` waits that could
    assert before a job finished on a loaded CI runner."""
    for _ in range(tries):
        if all(j.status != "running" for j in reg.list()):
            return
        await asyncio.sleep(0.005)
    raise AssertionError("jobs did not reach a terminal state in time")


@pytest.mark.anyio
async def test_register_returns_id_and_tracks_running():
    reg = JobRegistry()
    job_id = reg.register("agent", "explore: look", _sleep_then("done", 0.2))
    assert job_id == "job-1"
    job = reg.get(job_id)
    assert isinstance(job, Job)
    assert job.status == "running"
    assert job.kind == "agent"
    assert job.label == "explore: look"


@pytest.mark.anyio
async def test_ids_increment():
    reg = JobRegistry()
    a = reg.register("agent", "a", _sleep_then("x", 0.05))
    b = reg.register("agent", "b", _sleep_then("y", 0.05))
    assert (a, b) == ("job-1", "job-2")


@pytest.mark.anyio
async def test_job_completes_and_stores_result():
    reg = JobRegistry()
    job_id = reg.register("agent", "a", _sleep_then("the report", 0.01))
    result = await reg.wait(job_id)
    assert result == "the report"
    assert reg.get(job_id).status == "done"


@pytest.mark.anyio
async def test_wait_on_finished_job_returns_result():
    reg = JobRegistry()
    job_id = reg.register("agent", "a", _sleep_then("R", 0.01))
    await reg.wait(job_id)
    # waiting again returns the stored result without re-running
    assert await reg.wait(job_id) == "R"


@pytest.mark.anyio
async def test_wait_times_out_but_job_keeps_running():
    reg = JobRegistry()
    job_id = reg.register("agent", "a", _sleep_then("slow", 0.3))
    msg = await reg.wait(job_id, timeout=0.05)
    assert "still running" in msg
    assert reg.get(job_id).status == "running"  # not cancelled by the timeout
    assert await reg.wait(job_id, timeout=2) == "slow"  # finishes later


@pytest.mark.anyio
async def test_failing_job_marked_failed_with_message():
    reg = JobRegistry()

    async def boom():
        raise ValueError("kaboom")

    job_id = reg.register("agent", "a", boom())
    result = await reg.wait(job_id)
    assert reg.get(job_id).status == "failed"
    assert "kaboom" in result


@pytest.mark.anyio
async def test_cancel_stops_job():
    reg = JobRegistry()
    job_id = reg.register("agent", "a", _sleep_then("never", 5))
    msg = await reg.cancel(job_id)
    assert "cancel" in msg.lower()
    assert reg.get(job_id).status == "cancelled"


@pytest.mark.anyio
async def test_cancel_runs_kill_callback():
    reg = JobRegistry()
    killed = []
    job_id = reg.register(
        "bash", "sleep", _sleep_then("x", 5), kill=lambda: killed.append(True)
    )
    await reg.cancel(job_id)
    assert killed == [True]


@pytest.mark.anyio
async def test_cancel_unknown_and_finished():
    reg = JobRegistry()
    assert "no job" in (await reg.cancel("ghost")).lower()
    job_id = reg.register("agent", "a", _sleep_then("x", 0.01))
    await reg.wait(job_id)
    assert "already" in (await reg.cancel(job_id)).lower()


@pytest.mark.anyio
async def test_output_running_vs_done():
    reg = JobRegistry()
    job_id = reg.register("agent", "a", _sleep_then("final", 0.2))
    assert "running" in reg.output(job_id).lower()
    await reg.wait(job_id)
    assert reg.output(job_id) == "final"
    assert "no job" in reg.output("ghost").lower()


@pytest.mark.anyio
async def test_output_uses_live_buffer_for_bash_style_job():
    reg = JobRegistry()
    buf = ["line1\n"]
    job_id = reg.register(
        "bash", "cmd", _sleep_then("done", 0.2), output_fn=lambda: "".join(buf)
    )
    assert "line1" in reg.output(job_id)  # live buffer while running
    buf.append("line2\n")
    assert "line2" in reg.output(job_id)
    await reg.wait(job_id)


@pytest.mark.anyio
async def test_on_change_fires_on_launch_and_completion():
    calls = []
    reg = JobRegistry(on_change=lambda: calls.append(True))
    job_id = reg.register("agent", "a", _sleep_then("x", 0.01))
    assert len(calls) >= 1  # launch fired
    await reg.wait(job_id)
    assert len(calls) >= 2  # completion fired


@pytest.mark.anyio
async def test_list_returns_all_in_order():
    reg = JobRegistry()
    reg.register("agent", "a", _sleep_then("x", 0.01))
    reg.register("bash", "b", _sleep_then("y", 0.01))
    ids = [j.id for j in reg.list()]
    assert ids == ["job-1", "job-2"]


@pytest.mark.anyio
async def test_cancel_all_stops_every_running_job():
    reg = JobRegistry()
    reg.register("agent", "a", _sleep_then("x", 5))
    reg.register("agent", "b", _sleep_then("y", 5))
    await reg.cancel_all()
    assert all(j.status == "cancelled" for j in reg.list())


@pytest.mark.anyio
async def test_finished_digest_lists_completed_then_clears():
    reg = JobRegistry()
    reg.register("bash", "build", _sleep_then("ok", 0.01))
    reg.register("agent", "explore: map", _sleep_then("r", 0.01))
    await _settled(reg)
    digest = reg.take_finished_digest()
    assert "job-1 (bash) done" in digest
    assert "job-2 (agent) done" in digest
    # Draining clears the buffer.
    assert reg.take_finished_digest() == ""


@pytest.mark.anyio
async def test_finished_digest_empty_when_nothing_finished():
    reg = JobRegistry()
    assert reg.take_finished_digest() == ""
    reg.register("agent", "slow", _sleep_then("x", 5))
    assert reg.take_finished_digest() == ""  # still running
    await reg.cancel_all()


@pytest.mark.anyio
async def test_finished_digest_includes_result_tail():
    """The digest carries a tail of each finished job's output so the model reads
    the verdict inline, without spending a separate job_output pull."""
    reg = JobRegistry()
    result = (
        "exit 0\n"
        + "\n".join(f"noise{i}" for i in range(300))
        + "\n=== 717 passed in 12.3s ==="
    )
    reg.register("bash", "tests", _sleep_then(result, 0.01))
    await _settled(reg)
    digest = reg.take_finished_digest()
    assert "job-1 (bash) done" in digest
    assert "717 passed in 12.3s" in digest  # the verdict (tail) is inline


@pytest.mark.anyio
async def test_finished_digest_caps_result_tail():
    """A huge result is bounded in the digest — it keeps the tail (verdict) but
    doesn't dump the whole buffer into the next turn's prompt."""
    reg = JobRegistry()
    result = "x" * 5000 + "VERDICT-END"
    reg.register("bash", "big", _sleep_then(result, 0.01))
    await _settled(reg)
    digest = reg.take_finished_digest()
    assert "VERDICT-END" in digest  # tail kept
    assert len(digest) < 1000  # bounded, not the full 5000-char result


@pytest.mark.anyio
async def test_finished_digest_includes_cancelled_and_failed():
    reg = JobRegistry()
    slow = reg.register("bash", "sleep", _sleep_then("x", 5))
    await reg.cancel(slow)

    async def boom():
        raise ValueError("nope")

    reg.register("agent", "broken", boom())
    await _settled(reg)
    digest = reg.take_finished_digest()
    assert "job-1 (bash) cancelled" in digest
    assert "job-2 (agent) failed" in digest


def test_render_jobs_formats_rows():
    # Build Jobs directly to avoid scheduling for this pure-render test.
    jobs = [
        Job(id="job-1", kind="bash", label="pytest -x", status="running"),
        Job(id="job-2", kind="agent", label="explore: map", status="done"),
        Job(id="job-3", kind="bash", label="build", status="failed"),
    ]
    text = render_jobs(jobs)
    assert "job-1" in text and "pytest -x" in text
    assert "▸" in text and "+" in text and "x" in text
    assert render_jobs([]) == ""


@pytest.mark.anyio
async def test_cancel_before_start_closes_coroutine():
    """A job cancelled before its task ever runs must still consume the caller's
    coroutine (drive it to a closed state), not drop it un-started — otherwise
    Python emits a 'coroutine was never awaited' RuntimeWarning at GC."""
    from inspect import CORO_CLOSED, getcoroutinestate

    reg = JobRegistry()
    coro = _sleep_then("x", 5)
    reg.register("agent", "a", coro)  # no await before cancel -> task never starts
    await reg.cancel_all()

    assert getcoroutinestate(coro) == CORO_CLOSED


@pytest.mark.anyio
async def test_has_finished_pending_reflects_set_without_consuming():
    reg = JobRegistry()
    # Nothing finished yet.
    assert reg.has_finished_pending() is False
    reg.register("agent", "a", _sleep_then("R", 0.01))
    assert reg.has_finished_pending() is False  # still running
    await _settled(reg)
    # Finished -> pending, and checking it does NOT drain the digest.
    assert reg.has_finished_pending() is True
    assert reg.has_finished_pending() is True  # non-consuming
    digest = reg.take_finished_digest()
    assert "job-1 (agent) done" in digest  # the digest survived the peeks
    # Draining clears the pending flag.
    assert reg.has_finished_pending() is False


@pytest.mark.anyio
async def test_wait_consumes_finished_digest():
    """wait_for_job marks the job as wake-consumed so the wake scheduler
    won't fire a redundant turn, but the digest is preserved for the model."""
    reg = JobRegistry()
    job_id = reg.register("bash", "echo", _sleep_then("hello", 0.01))
    assert reg.has_finished_pending() is False
    result = await reg.wait(job_id)
    assert result == "hello"
    # Wake-consumed: has_finished_pending returns False for wake scheduler.
    assert reg.has_finished_pending() is False
    # But digest is still there for the model.
    digest = reg.take_finished_digest()
    assert "job-1 (bash) done" in digest


@pytest.mark.anyio
async def test_wait_already_finished_consumes_digest():
    """wait on an already-finished job also marks as wake-consumed."""
    reg = JobRegistry()
    job_id = reg.register("bash", "echo", _sleep_then("done", 0.01))
    await _settled(reg)
    assert reg.has_finished_pending() is True
    result = await reg.wait(job_id)
    assert result == "done"
    assert reg.has_finished_pending() is False  # wake-consumed
    digest = reg.take_finished_digest()
    assert "job-1 (bash) done" in digest  # digest preserved


@pytest.mark.anyio
async def test_output_marks_finished_job_consumed():
    """Reading a finished job's output with mark_seen=True marks it
    wake-consumed (like wait), so the wake scheduler won't fire a redundant
    turn — the agent already has the result. The digest is preserved."""
    reg = JobRegistry()
    job_id = reg.register("agent", "review", _sleep_then("all tests pass", 0.01))
    await _settled(reg)
    assert reg.has_finished_pending() is True
    assert reg.output(job_id, mark_seen=True) == "all tests pass"
    assert reg.has_finished_pending() is False  # wake-consumed
    assert "job-1 (agent) done" in reg.take_finished_digest()


@pytest.mark.anyio
async def test_output_without_mark_seen_leaves_wake_pending():
    """A passive read (the TUI /jobs command) must not mark the job consumed:
    the agent still hasn't reacted, so its wake should still fire."""
    reg = JobRegistry()
    job_id = reg.register("agent", "review", _sleep_then("done", 0.01))
    await _settled(reg)
    assert reg.output(job_id) == "done"  # default: mark_seen=False
    assert reg.has_finished_pending() is True  # wake still pending


@pytest.mark.anyio
async def test_output_on_running_job_does_not_consume():
    """mark_seen only consumes terminal jobs. Reading a still-running job's
    live output leaves the wake to fire when it finishes."""
    reg = JobRegistry()
    job_id = reg.register("agent", "slow", _sleep_then("R", 0.2))
    reg.output(job_id, mark_seen=True)  # still running -> nothing to consume
    await _settled(reg)  # now it finishes (unseen result)
    assert reg.has_finished_pending() is True


@pytest.mark.anyio
async def test_cancel_consumes_wake_digest():
    """Cancelling a job marks it wake-consumed: the agent initiated the cancel,
    so it must not trigger a redundant autonomous wake. The digest still records
    the cancellation for the model's next turn."""
    reg = JobRegistry()
    job_id = reg.register("agent", "slow", _sleep_then("never", 5))
    result = await reg.cancel(job_id)
    assert "cancelled" in result
    assert reg.has_finished_pending() is False  # wake-consumed
    digest = reg.take_finished_digest()
    assert "job-1 (agent) cancelled" in digest  # still surfaced to the model


def _sleep_then(value, seconds):
    async def coro():
        await asyncio.sleep(seconds)
        return value

    return coro()
