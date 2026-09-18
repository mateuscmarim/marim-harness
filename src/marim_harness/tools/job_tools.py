from typing import Literal

from pydantic_ai import RunContext

from ..jobs import render_jobs
from ..runtime.deps import Deps

_POLL_WAKE_NOTE = "(running jobs wake you on completion — no need to check again)"
_POLL_WARN = (
    "⚠ No change since your last check. If you have no other work, end your "
    "turn — finished jobs wake you and deliver their reports automatically."
)
_POLL_WARN_HEADLESS = (
    "⚠ No change since your last check. Use wait_for_job(id) to block until a "
    "job finishes instead of polling."
)


def _guarded_poll_response(ctx: RunContext[Deps], key: str, body: str, *, any_running: bool) -> str:
    """Apply the poll guard (spec 2026-07-02-job-poll-guard-design) to one
    read-only jobs response. Counts only while something still runs — reading
    settled results is never polling. Interactive sessions escalate: the 2nd
    identical look appends a warning, the 3rd+ replaces the body entirely (a
    wake loop exists, so ending the turn is always safe, and a fresh-looking
    table makes the warning read as boilerplate). Headless has no wake loop and
    may still need the data: append-only, pointing at wait_for_job."""
    if not any_running:
        return body
    count = ctx.deps.jobs.note_poll(key, body)
    if count < 2:
        return body
    if not ctx.deps.ui.interactive:
        return f"{body}\n\n{_POLL_WARN_HEADLESS}"
    if count == 2:
        return f"{body}\n\n{_POLL_WARN}"
    return (
        f"No change since your last check (poll {count}). Stop polling: end "
        "your turn now — finished jobs wake you and deliver their reports "
        "automatically. Use wait_for_job(id) only if you must block on a "
        "result inside this turn."
    )


def _jobs_listing(ctx: RunContext[Deps]) -> str:
    """The shared body of jobs() and job("list"): the rendered table, with the
    standing wake note while anything runs (interactive only — headless has no
    wake loop), passed through the poll guard. render_jobs output is a stable
    projection (no elapsed times), so it doubles as the poll snapshot."""
    listed = ctx.deps.jobs.list()
    rows = render_jobs(listed)
    if not rows:
        return "No background jobs."
    any_running = any(j.status == "running" for j in listed)
    if any_running and ctx.deps.ui.interactive:
        rows = f"{rows}\n{_POLL_WAKE_NOTE}"
    return _guarded_poll_response(ctx, "list", rows, any_running=any_running)


def _job_output_read(ctx: RunContext[Deps], id: str) -> str:
    """The shared body of job_output() and job("output"): the read, passed
    through the poll guard keyed per job while that job still runs. A growing
    bash buffer changes the snapshot every call, so real progress is never
    nagged — only zero-information repeats are."""
    target = ctx.deps.jobs.get(id)
    body = ctx.deps.jobs.output(id, mark_seen=True)
    running = target is not None and target.status == "running"
    return _guarded_poll_response(ctx, f"output:{id}", body, any_running=running)


_WAIT_TIMEOUT_NUDGE = (
    "If you don't need its result to continue this turn, end your turn — the "
    "harness wakes you when it finishes and delivers its report. Wait again "
    "only if you must block on it now."
)
_WAIT_RELEASED_NUDGE = (
    "The job did NOT finish. The user's new message is included in this "
    "request — read it and act on it first. The job keeps running: wait on it "
    "again when you need its result, or end your turn and its report is "
    "delivered when it finishes."
)


async def _job_wait(ctx: RunContext[Deps], id: str, timeout: float | None) -> str:
    """The shared body of wait_for_job() and job("wait"). The registry says how
    the wait ended: a steer-released wait gets a read-the-guidance note in
    every mode (the guidance is what ended it), while an explicit timeout gets
    the end-your-turn nudge only in interactive sessions — the wake loop makes
    that the cheaper move there, whereas headless has no wake loop and
    re-waiting IS the right call, so it keeps the bare note. Softer than the
    poll guard on purpose: a timed-out wait sometimes precedes a legitimate
    re-wait mid-task."""
    outcome = await ctx.deps.jobs.wait_outcome(id, timeout)
    if outcome.kind == "released":
        return f"{outcome.text}\n\n{_WAIT_RELEASED_NUDGE}"
    if outcome.kind == "timeout" and ctx.deps.ui.interactive:
        return f"{outcome.text}\n\n{_WAIT_TIMEOUT_NUDGE}"
    return outcome.text


def jobs(ctx: RunContext[Deps]) -> str:
    """List the background jobs you've launched this session, with their id, kind
    (bash/agent), label, and status (running/done/failed/cancelled). Use this to
    see what's still in flight before pulling results with job_output or
    wait_for_job. Never call this in a loop to wait — if you have no other work,
    end your turn; the harness wakes you when a job finishes and delivers its
    report."""
    return _jobs_listing(ctx)


def job_output(ctx: RunContext[Deps], id: str) -> str:
    """Read a background job's output by id without blocking: the final result if
    it's finished, the live output so far for a running bash job, or a running
    marker otherwise. To block until a job finishes, use wait_for_job instead."""
    return _job_output_read(ctx, id)


async def wait_for_job(ctx: RunContext[Deps], id: str, timeout: float | None = None) -> str:
    """Block until a background job finishes, then return its result. By default
    there is no timeout: one call waits through completion, however long the
    job takes — never re-wait in a loop. Pass `timeout` (seconds — unlike
    bash's millisecond timeout) only if you need a bounded block; on expiry the
    job keeps going and you get a "still running" note. If the user sends new
    guidance while you wait, the wait is released early with a note saying so:
    the job is still running, the user's message is in that same request —
    act on it, then wait again or end your turn (the report is delivered when
    the job finishes). Use this only when you must block on a job's result
    before continuing; otherwise end your turn and the harness wakes you with
    the report. To make progress meanwhile, emit independent read_file/grep
    calls in the SAME response as this wait — they run concurrently while the
    job finishes."""
    return await _job_wait(ctx, id, timeout)


async def cancel_job(ctx: RunContext[Deps], id: str) -> str:
    """Stop a running background job by id: kills its process (bash) or cancels
    its run (agent). Finished jobs are left as-is."""
    return await ctx.deps.jobs.cancel(id)


async def job(
    ctx: RunContext[Deps],
    action: Literal["list", "output", "wait", "cancel"],
    id: str = "",
    timeout: float | None = None,
) -> str:
    """Manage background jobs you've launched this session. `action`:
    - "list": show every job with its id, kind (bash/agent), label, and status.
    - "output": read job `id`'s output without blocking — final result if done,
      live output so far for a running bash job.
    - "wait": block until job `id` finishes and return its result. No timeout
      by default — one call waits through completion; never re-wait in a
      loop. Pass `timeout` (seconds) only for a bounded block: on expiry the
      job keeps going and you get a still-running note. If the user sends new
      guidance while you wait, the wait is released early with a note saying
      so — the job is still running and the user's message is in that same
      request; act on it, then wait again or end your turn (the report is
      delivered when it finishes).
    - "cancel": stop running job `id` (kills its process or cancels its run).
    `id` is required for every action except "list"; `timeout` applies only to
    "wait" and is in seconds (unlike bash's millisecond timeout). Never call
    "list" or "output" in a loop to wait — if you have no other work, end your
    turn; the harness wakes you when a job finishes and delivers its report."""
    if action == "list":
        return _jobs_listing(ctx)
    if not id:
        return f'job: action {action!r} needs an id (use action="list" to find it).'
    if action == "output":
        return _job_output_read(ctx, id)
    if action == "wait":
        return await _job_wait(ctx, id, timeout)
    return await ctx.deps.jobs.cancel(id)  # action == "cancel"
