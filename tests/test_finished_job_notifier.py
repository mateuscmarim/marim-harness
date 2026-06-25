from dataclasses import dataclass

from marim_harness.interfaces.tui.notify import FinishedJobNotifier


@dataclass
class _FakeJob:
    id: str
    kind: str
    status: str


def test_returns_done_and_failed_once_each():
    n = FinishedJobNotifier()
    jobs = [
        _FakeJob("a", "bash", "done"),
        _FakeJob("b", "agent", "failed"),
        _FakeJob("c", "bash", "running"),
    ]
    fresh = n.newly_finished(jobs)
    assert {j.id for j in fresh} == {"a", "b"}  # running excluded


def test_each_job_notified_only_once_across_calls():
    n = FinishedJobNotifier()
    jobs = [_FakeJob("a", "bash", "done")]
    assert [j.id for j in n.newly_finished(jobs)] == ["a"]
    # Same job, polled again after it's already been notified.
    assert n.newly_finished(jobs) == []


def test_cancelled_jobs_are_never_returned():
    n = FinishedJobNotifier()
    jobs = [_FakeJob("a", "agent", "cancelled")]
    assert n.newly_finished(jobs) == []
    assert "a" not in n.notified


def test_running_then_done_returned_when_it_finishes():
    n = FinishedJobNotifier()
    job = _FakeJob("a", "bash", "running")
    assert n.newly_finished([job]) == []
    job.status = "done"
    assert [j.id for j in n.newly_finished([job])] == ["a"]
