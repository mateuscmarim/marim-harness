"""Pure assembly of the jobs-view wire DTOs from the in-memory JobRegistry
state plus the persisted per-spawn sidecar meta. No I/O, no Starlette — the
HTTP handlers in ``http.py`` supply the live jobs and a ``read_meta`` closure
and serialize the result. Kept separate so the shaping logic is unit-tested
without spinning a server."""

from __future__ import annotations

from collections.abc import Callable

from ..jobs import Job


def job_to_dto(job: Job, meta: dict | None) -> dict:
    """One JobDto. ``meta`` is the spawn's sidecar meta (agent jobs) or None.
    Metric fields stay ``None`` when meta is absent (bash jobs, or an agent
    spawn still running before its terminal meta is written)."""
    usage = meta.get("usage") if meta else None
    tool_count = meta.get("tool_count") if meta else None
    duration = meta.get("duration") if meta else None
    return {
        "id": job.id,
        "kind": job.kind,
        "label": job.label,
        "status": job.status,
        "started_at": job.started_at,
        "finished_at": job.finished_at,
        "stream_id": job.stream_id,
        "duration_secs": duration,
        "usage": usage,
        "tool_count": tool_count,
        # Detail-only field: the list DTO never carries the actual prompt (it
        # can be long and isn't needed for the jobs panel); detail_dto()
        # overwrites this placeholder with job.prompt.
        "prompt": None,
    }


def _meta_for(job: Job, read_meta: Callable[[str], dict | None]) -> dict | None:
    if job.kind == "agent" and job.stream_id:
        return read_meta(job.stream_id)
    return None


def assemble(
    jobs: list[Job],
    history: list[Job],
    read_meta: Callable[[str], dict | None],
) -> list[dict]:
    """Every job as a JobDto: running first, then settled by ``finished_at``
    descending (missing timestamps sort last). ``jobs`` are this process's live
    registry rows; ``history`` are prior-session settled summaries — disjoint
    ids, so no dedup is needed."""
    combined = list(jobs) + list(history)
    running = [j for j in combined if j.status == "running"]
    settled = [j for j in combined if j.status != "running"]
    settled.sort(key=lambda j: j.finished_at or "", reverse=True)
    return [job_to_dto(j, _meta_for(j, read_meta)) for j in running + settled]


def detail_dto(job: Job, result: str, meta: dict | None) -> dict:
    """A JobDto plus the drill-in fields: the spawn ``prompt`` (input) and the
    full ``result`` (output)."""
    dto = job_to_dto(job, meta)
    dto["prompt"] = job.prompt
    dto["result"] = result
    return dto
