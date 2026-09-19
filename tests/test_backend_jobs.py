"""Backend-owned agents are visible even with no active transcript consumer."""

import pytest

from marim_harness.jobs import JobRegistry
from marim_harness.runtime.backend_jobs import ClaudeJobObserver


def test_claude_agent_launch_is_not_completion_and_report_arrives_between_turns():
    jobs = JobRegistry()
    observer = ClaudeJobObserver(jobs)
    observer(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent-1",
                        "name": "Agent",
                        "input": {"prompt": "Implement files", "description": "File support"},
                    }
                ]
            },
        }
    )
    observer({"type": "system", "subtype": "task_started", "tool_use_id": "agent-1"})
    observer(
        {
            "type": "user",
            "message": {
                "content": [
                    {
                        "type": "tool_result",
                        "tool_use_id": "agent-1",
                        "content": "Async agent launched",
                    }
                ]
            },
        }
    )
    observer({"type": "result"})
    [job] = jobs.list()
    assert job.status == "running" and job.prompt == "Implement files"
    observer(
        {
            "type": "system",
            "subtype": "task_notification",
            "tool_use_id": "agent-1",
            "status": "completed",
            "summary": "Implemented file support",
        }
    )
    assert job.status == "done" and job.result == "Implemented file support"
    assert jobs.take_finished_digest() == ""


@pytest.mark.parametrize("first_status", ["completed", "failed", "stopped"])
def test_claude_task_started_reactivates_finished_agent_without_another_agent_call(first_status):
    from tests.test_cli_demux import _spawn_obj

    changes = []
    jobs = JobRegistry(on_change=lambda: changes.append(True))
    observer = ClaudeJobObserver(jobs)
    observer(_spawn_obj())
    started = {
        "type": "system",
        "subtype": "task_started",
        "tool_use_id": "t1",
        "task_id": "agent-1",
        "is_backgrounded": True,
    }
    finished = {
        "type": "system",
        "subtype": "task_notification",
        "tool_use_id": "t1",
        "task_id": "agent-1",
        "status": first_status,
        "summary": "First report",
    }
    observer(started)
    observer(finished)
    [job] = jobs.list()
    assert (
        job.status
        == {"completed": "done", "failed": "failed", "stopped": "cancelled"}[first_status]
    )
    # SendMessage resumes the existing task; there is no new Agent tool_use.
    observer(started)
    observer(started)
    assert jobs.list() == [job] and job.status == "running"
    assert job.result is None
    observer({**finished, "status": "completed", "summary": "Second report"})
    observer({**finished, "status": "completed", "summary": "Second report"})
    assert job.status == "done" and job.result == "Second report"
    assert len(changes) == 4
    assert jobs.take_finished_digest() == ""


@pytest.mark.parametrize("factory", [ClaudeJobObserver])
def test_tracking_closure_is_terminal_and_late_events_cannot_reopen(factory):
    jobs = JobRegistry()
    observer = factory(jobs)
    observer(
        {
            "type": "assistant",
            "message": {
                "content": [
                    {
                        "type": "tool_use",
                        "id": "agent-1",
                        "name": "Agent",
                        "input": {},
                    }
                ]
            },
        }
    )
    observer.close()
    [job] = jobs.list()
    assert job.status == "failed"
    assert "unavailable" in job.result
    assert not jobs.has_finished_pending()
