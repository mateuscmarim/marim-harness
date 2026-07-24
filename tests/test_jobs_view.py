from marim_harness.jobs import Job
from marim_harness.server.jobs_view import assemble, detail_dto, job_to_dto


def _agent(id, status, finished_at=None):
    return Job(id=id, kind="agent", label=f"explore: {id}", status=status,
               result="r", stream_id=f"stream-{id}", finished_at=finished_at,
               started_at="2026-07-23T00:00:00+00:00", prompt="do the thing")


def test_job_to_dto_enriches_agent_meta():
    dto = job_to_dto(_agent("job-1", "done", "2026-07-23T00:05:00+00:00"),
                     {"usage": {"input": 10, "output": 20}, "tool_count": 3, "duration": 12.5})
    assert dto["kind"] == "agent"
    assert dto["usage"] == {"input": 10, "output": 20}
    assert dto["tool_count"] == 3
    assert dto["duration_secs"] == 12.5
    assert dto["prompt"] is None  # detail-only, never on the list DTO


def test_job_to_dto_null_meta_leaves_metrics_none():
    dto = job_to_dto(Job(id="job-2", kind="bash", label="ls", status="running",
                         started_at="2026-07-23T00:00:00+00:00"), None)
    assert dto["usage"] is None and dto["tool_count"] is None and dto["duration_secs"] is None
    assert dto["kind"] == "bash"


def test_assemble_orders_running_first_then_finished_desc():
    jobs = [
        _agent("job-3", "done", "2026-07-23T00:01:00+00:00"),
        _agent("job-4", "running"),
        _agent("job-5", "done", "2026-07-23T00:09:00+00:00"),
    ]
    out = assemble(jobs, history=[], read_meta=lambda sid: None)
    assert [d["id"] for d in out] == ["job-4", "job-5", "job-3"]


def test_assemble_reads_meta_only_for_agent_stream_ids():
    seen = []

    def reader(sid):
        seen.append(sid)
        return None

    bash = Job(id="job-6", kind="bash", label="ls", status="done",
               finished_at="2026-07-23T00:02:00+00:00")
    assemble([bash, _agent("job-7", "done", "2026-07-23T00:03:00+00:00")],
             history=[], read_meta=reader)
    assert seen == ["stream-job-7"]  # bash (no stream_id) never triggers a read


def test_detail_dto_carries_prompt_and_result():
    dto = detail_dto(_agent("job-8", "done", "2026-07-23T00:04:00+00:00"),
                     result="the full output", meta=None)
    assert dto["prompt"] == "do the thing"
    assert dto["result"] == "the full output"
    assert dto["id"] == "job-8"
