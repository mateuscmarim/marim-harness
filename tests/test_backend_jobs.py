"""Backend-owned agents are visible even with no active transcript consumer."""

import pytest

from marim_harness.jobs import JobRegistry
from marim_harness.runtime.backend_jobs import ClaudeJobObserver, CodexJobObserver


def ping(kind, *, thread="parent", child="child", call="spawn-1"):
    return "item/started", {
        "threadId": thread,
        "item": {
            "type": "subAgentActivity",
            "id": call,
            "agentThreadId": child,
            "agentPath": "/root/server_files",
            "kind": kind,
        },
    }


def test_codex_agents_remain_visible_after_parent_finishes_and_settle_while_idle():
    changes = []
    jobs = JobRegistry(on_change=lambda: changes.append(True))
    observer = CodexJobObserver(jobs, "parent")
    observer(*ping("started"))
    observer(*ping("started"))
    [job] = jobs.list()
    assert job.label == "server_files" and job.status == "running"
    assert job.stream_id == "spawn-1"
    observer("turn/completed", {"threadId": "parent", "turn": {"status": "completed"}})
    assert job.status == "running"
    observer(
        "item/agentMessage/delta",
        {
            "threadId": "child",
            "itemId": "answer",
            "delta": "Server route implemented.",
        },
    )
    observer(*ping("completed"))
    observer(*ping("completed"))
    assert job.status == "done" and job.result == "Server route implemented."
    assert len(changes) == 2
    assert jobs.take_finished_digest() == ""
    assert not jobs.has_finished_pending()


def test_codex_nested_agents_and_unknown_threads_are_scoped():
    jobs = JobRegistry()
    observer = CodexJobObserver(jobs, "parent")
    observer(*ping("started", thread="unrelated"))
    assert jobs.list() == []
    observer(*ping("started"))
    observer(*ping("started", thread="child", child="grandchild", call="spawn-2"))
    assert len(jobs.list()) == 2
    observer(*ping("interrupted", thread="child", child="grandchild", call="spawn-2"))
    assert jobs.list()[0].status == "running"
    assert jobs.list()[1].status == "cancelled"


def test_reused_codex_child_turn_reactivates_same_job_without_parent_collab_call():
    changes = []
    jobs = JobRegistry(on_change=lambda: changes.append(True))
    observer = CodexJobObserver(jobs, "parent")
    observer(*ping("started"))
    observer(
        "item/agentMessage/delta",
        {
            "threadId": "child",
            "itemId": "old",
            "delta": "First report",
        },
    )
    observer(*ping("completed"))
    [job] = jobs.list()
    assert job.status == "done"
    # Codex can reuse its native agent without sending a collabAgentToolCall.
    # Interaction alone (wait/message) does not prove it is running again.
    observer(*ping("interacted"))
    assert job.status == "done"
    observer("turn/started", {"threadId": "child", "turn": {"id": "next"}})
    observer("turn/started", {"threadId": "child", "turn": {"id": "next"}})
    assert jobs.list() == [job] and job.status == "running"
    assert job.result is None
    observer(*ping("completed"))
    assert job.status == "done" and "First report" not in job.result
    assert len(changes) == 4
    assert jobs.take_finished_digest() == ""


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


@pytest.mark.parametrize("factory", [lambda j: CodexJobObserver(j, "parent"), ClaudeJobObserver])
def test_tracking_closure_is_terminal_and_late_events_cannot_reopen(factory):
    jobs = JobRegistry()
    observer = factory(jobs)
    if isinstance(observer, CodexJobObserver):
        observer(*ping("started"))
    else:
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


@pytest.mark.anyio
async def test_codex_reader_updates_idle_jobs_without_consuming_or_crossing_session_queues():
    from marim_harness.codex.server import CodexServer

    async def request_handler(*_):
        return {}

    server = CodexServer()
    first = server._register({"id": "parent"}, request_handler)
    second = server._register({"id": "other"}, request_handler)
    jobs, other_jobs = JobRegistry(), JobRegistry()
    first.on_observation = CodexJobObserver(jobs, first.thread_id)
    second.on_observation = CodexJobObserver(other_jobs, second.thread_id)
    await server._on_notification(*ping("started"))
    [job] = jobs.list()
    await server._on_notification(
        "item/agentMessage/delta",
        {
            "threadId": "child",
            "itemId": "answer",
            "delta": "Finished while idle.",
        },
    )
    await server._on_notification(*ping("completed"))
    assert job.status == "done" and job.result == "Finished while idle."
    assert other_jobs.list() == [] and second.events.empty()
    assert first.events.qsize() == 3  # original events still belong to the normal consumer
    await server._on_notification(*ping("started", child="child-2", call="spawn-2"))
    server.drop_thread(first)
    assert jobs.list()[1].status == "failed"
    assert "other" in server.thread_ids


def test_terminal_callback_is_once_and_never_flushes_on_parent_completion():
    persisted = []
    jobs = JobRegistry()
    observer = CodexJobObserver(jobs, "parent", lambda: persisted.append(jobs.export_settled()))
    observer(*ping("started"))
    observer("turn/completed", {"threadId": "parent", "turn": {"status": "completed"}})
    assert persisted == []
    observer(*ping("completed"))
    observer(*ping("completed"))
    assert len(persisted) == 1 and persisted[0][0]["status"] == "done"


@pytest.mark.parametrize(
    "status,expected",
    [
        ("interrupted", "cancelled"),
        ("shutdown", "cancelled"),
        ("errored", "failed"),
        ("notFound", "failed"),
        ("completed", "done"),
    ],
)
def test_codex_collab_states_preserve_the_actual_terminal_outcome(status, expected):
    jobs = JobRegistry()
    observer = CodexJobObserver(jobs, "parent")
    observer(*ping("started"))
    observer(*ping("started", thread="child", child="grandchild", call="spawn-2"))
    observer(
        "item/completed",
        {
            "threadId": "parent",
            "item": {
                "id": "wait-1",
                "type": "collabAgentToolCall",
                "tool": "wait",
                "status": "completed",
                "receiverThreadIds": ["child"],
                "agentsStates": {"child": {"status": status}},
            },
        },
    )
    assert jobs.list()[0].status == expected
    if status in ("shutdown", "notFound"):
        assert jobs.list()[1].status == "failed"
    else:
        assert jobs.list()[1].status == "running"


def test_old_observer_cannot_write_jobs_after_a_session_rebind():
    jobs = JobRegistry()
    observer = CodexJobObserver(jobs, "parent")
    observer(*ping("started"))
    jobs.clear_history()
    jobs.import_history([{"id": "job-40", "status": "done", "backend_owned": True}])
    jobs.discard_observed()
    observer(*ping("started", child="late", call="late-spawn"))
    observer(*ping("completed"))
    observer.close()
    assert jobs.list() == []
    assert jobs.history[0].id == "job-40"
    current = CodexJobObserver(jobs, "new-parent")
    current(*ping("started", thread="new-parent"))
    assert len(jobs.list()) == 1 and jobs.list()[0].status == "running"
