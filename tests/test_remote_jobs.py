"""``JobMirror`` (phase 4b): the daemon's jobs as read over the wire, behind
the same read surface the panel and the replay settle use in process."""

from __future__ import annotations

from marim_harness.interfaces.tui.remote_jobs import JobMirror
from marim_harness.jobs import Job


def _job(job_id: str, status: str, *, result: str | None = None, stream_id: str | None = None):
    return Job(
        id=job_id, kind="agent", label=job_id, status=status, result=result, stream_id=stream_id
    )  # type: ignore[arg-type]


def test_mirror_is_unsynced_until_a_read_lands():
    mirror = JobMirror()
    assert not mirror.synced
    assert mirror.list() == [] and mirror.history == []
    assert not mirror.any_running() and not mirror.has_finished_pending()
    mirror.apply([_job("job-1", "running", stream_id="sg-1")])
    assert mirror.synced
    assert mirror.any_running()
    assert mirror.get("job-1") is not None and mirror.get("job-2") is None
    # Every row is in list(); history stays empty so the panel's
    # ``history + list()`` paint shows each once.
    assert [j.id for j in mirror.list()] == ["job-1"]
    assert mirror.history == []
    # Autonomous wake belongs to the daemon: never a finished-pending here.
    mirror.apply([_job("job-1", "done", result="tail", stream_id="sg-1")])
    assert not mirror.has_finished_pending()


def test_apply_replaces_the_snapshot_in_the_daemons_order():
    mirror = JobMirror()
    mirror.apply([_job("job-1", "running"), _job("job-2", "done")])
    mirror.apply([_job("job-3", "running"), _job("job-1", "done")])
    assert [j.id for j in mirror.list()] == ["job-3", "job-1"]
    assert mirror.get("job-2") is None


def test_full_results_survive_later_tail_only_reads():
    """A settled card is filled with the full result once; a later list read
    (tails only) neither downgrades it nor asks for it again."""
    mirror = JobMirror()
    mirror.apply([_job("job-1", "running", stream_id="sg-1"), _job("job-2", "running")])
    assert mirror.settled_needing_result({"job-1", "job-2"}) == []
    mirror.apply([_job("job-1", "done", result="…tail", stream_id="sg-1"), _job("job-2", "done")])
    # Only the cards still mapped (detached, waiting) are worth a fetch.
    assert mirror.settled_needing_result({"job-1"}) == ["job-1"]
    mirror.set_result("job-1", "the whole result")
    assert mirror.get("job-1").result == "the whole result"  # type: ignore[union-attr]
    assert mirror.settled_needing_result({"job-1"}) == []
    mirror.apply([_job("job-1", "done", result="…tail", stream_id="sg-1")])
    assert mirror.get("job-1").result == "the whole result"  # type: ignore[union-attr]
    # A result recorded for a row not (yet) in the snapshot is kept for it.
    mirror.set_result("job-7", "late")
    mirror.apply([_job("job-7", "failed", result="…l")])
    assert mirror.get("job-7").result == "late"  # type: ignore[union-attr]
