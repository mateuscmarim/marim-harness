"""Background jobs: a per-session, in-memory registry of detached work the agent
launches and later inspects.

Two kinds of work share one lifecycle — a shell process (``bash``) and an
isolated agent run (``agent``). The registry is agnostic to *how* either runs: it
wraps an awaitable that yields the final text, tracks status around it
(``running`` → ``done`` | ``failed`` | ``cancelled``), and knows how to stop it
(cancel the task, plus an optional ``kill`` for the OS process). Live output for
a running job comes from an optional ``output_fn`` (a bash job's growing buffer);
agent jobs have none and read ``(still running)`` until done.

State lives on :class:`~marim_harness.deps.Deps` next to the task checklist:
tools mutate it via ``ctx.deps.jobs``, and the TUI subscribes to ``on_change`` to
repaint a live panel. Nothing is persisted — jobs belong to the running process
and are cancelled on exit. The agent reaches results by *pulling*
(``job_output`` / ``wait_for_job``); nothing wakes a turn on its own.
"""

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Literal, Optional

logger = logging.getLogger(__name__)

Status = Literal["running", "done", "failed", "cancelled"]

_GLYPH = {"running": "▸", "done": "+", "failed": "x", "cancelled": "x"}

# How many trailing chars of a finished job's output to inline in the next-turn
# digest. The tail carries the verdict (a test summary, a final error), so a
# short tail lets the model read the result without a separate job_output pull,
# while the cap keeps the prompt from ballooning when many jobs finish at once.
_DIGEST_RESULT_CHARS = 200


@dataclass
class Job:
    """One background job. ``result`` holds the final output once finished (or the
    error text when failed); ``task`` is the wrapper coroutine task; ``kill`` and
    ``output_fn`` are the kind-specific hooks the registry calls."""

    id: str
    kind: str  # "bash" | "agent"
    label: str
    status: Status = "running"
    result: Optional[str] = None
    task: Optional[asyncio.Task] = field(default=None, repr=False)
    kill: Optional[Callable[[], None]] = field(default=None, repr=False)
    output_fn: Optional[Callable[[], str]] = field(default=None, repr=False)


class JobRegistry:
    """The session's live background jobs. Mutated in place so the TUI's reference
    and ``on_change`` wiring survive across session switches."""

    def __init__(self, on_change: Optional[Callable[[], None]] = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0
        self.on_change = on_change
        # Ids of jobs that reached a terminal state since the digest was last
        # drained — surfaced to the model at the start of its next turn so a
        # fire-and-forget result is never silently forgotten.
        self._finished_since_turn: list[str] = []

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _settle(self, job: Job, status: Status, result: Optional[str] = None) -> None:
        """Move a running job to its terminal ``status`` exactly once: record it
        for the next-turn digest and repaint. A no-op if already terminal, so the
        wrapper's cancel path and an explicit ``cancel()`` can't double-count."""
        if job.status != "running":
            return
        job.status = status
        if result is not None:
            job.result = result
        self._finished_since_turn.append(job.id)
        self._notify()

    def _next_id(self) -> str:
        self._counter += 1
        return f"job-{self._counter}"

    def register(
        self,
        kind: str,
        label: str,
        coro: Awaitable[str],
        *,
        kill: Optional[Callable[[], None]] = None,
        output_fn: Optional[Callable[[], str]] = None,
    ) -> str:
        """Schedule ``coro`` as a background job and return its id. The coroutine's
        return value becomes the job's result; an exception marks it failed; being
        cancelled marks it cancelled. Fires ``on_change`` on launch and finish."""
        job = Job(id=self._next_id(), kind=kind, label=label,
                  kill=kill, output_fn=output_fn)

        # Drive the caller's coroutine directly as the task and settle from a
        # done-callback. A wrapper coroutine that merely `await`s ``coro`` would,
        # if cancelled before it ever ran, drop ``coro`` un-started and unawaited
        # (a "coroutine was never awaited" leak); making ``coro`` itself the task
        # means asyncio closes it cleanly even on a cancel-before-start.
        task = asyncio.ensure_future(coro)

        def _on_done(t: "asyncio.Task") -> None:
            if t.cancelled():
                self._settle(job, "cancelled")
                return
            exc = t.exception()
            if exc is not None:  # a job failure never escapes into the loop
                self._settle(job, "failed", f"{type(exc).__name__}: {exc}")
            else:
                self._settle(job, "done", t.result())

        task.add_done_callback(_on_done)
        job.task = task
        self._jobs[job.id] = job
        self._notify()
        return job.id

    def get(self, job_id: str) -> Optional[Job]:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """Every job, in launch order."""
        return list(self._jobs.values())

    def output(self, job_id: str) -> str:
        """The job's output: the final result once finished, or the live buffer
        (bash) / a running marker while it's still going."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        if job.status == "running":
            if job.output_fn is not None:
                return job.output_fn() or "(running, no output yet)"
            return "(still running)"
        return job.result or ""

    async def wait(self, job_id: str, timeout: float = 60) -> str:
        """Block until the job finishes or ``timeout`` elapses, then return its
        result. A timeout leaves the job running (it isn't cancelled)."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        if job.status != "running" or job.task is None:
            return job.result if job.result is not None else f"({job.status})"
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout)
        except asyncio.TimeoutError:
            return f"job {job_id} still running after {timeout:g}s"
        except asyncio.CancelledError:
            pass  # the job itself was cancelled while we waited
        except Exception as exc:
            logger.debug("wait for job %s: %s (already settled)", job_id, exc)
        return job.result if job.result is not None else f"({job.status})"

    async def cancel(self, job_id: str) -> str:
        """Stop a running job: kill its OS process if any, then cancel the task."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        if job.status != "running":
            return f"job {job_id} already {job.status}"
        if job.kill is not None:
            job.kill()
        if job.task is not None:
            job.task.cancel()
            try:
                await job.task
            except (asyncio.CancelledError, Exception):
                pass
        # A task cancelled before it began running never hits the wrapper's
        # except, so settle here; _settle is a no-op if it already landed.
        self._settle(job, "cancelled")
        return f"cancelled {job_id}"

    async def cancel_all(self) -> None:
        """Cancel every running job (called on shutdown)."""
        for job in list(self._jobs.values()):
            if job.status == "running":
                await self.cancel(job.id)

    def _digest_tail(self, job: Job) -> str:
        """A ``: <tail>`` snippet for a finished job's digest line — the last
        :data:`_DIGEST_RESULT_CHARS` chars of its result, whitespace-collapsed so
        the verdict reads on one line. Empty when the job has no result (e.g.
        cancelled)."""
        if not job.result:
            return ""
        compact = " ".join(job.result.split())
        if len(compact) > _DIGEST_RESULT_CHARS:
            compact = "…" + compact[-_DIGEST_RESULT_CHARS:]
        return f": {compact}"

    def take_finished_digest(self) -> str:
        """Summary of jobs that finished since this was last called, then clear the
        buffer. Empty string when nothing finished. Each line carries a tail of the
        job's output so the verdict is readable inline; the Harness prepends this to
        the next turn so the model notices completions it didn't wait on."""
        ids = self._finished_since_turn
        self._finished_since_turn = []
        parts = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is not None:
                parts.append(f"{job.id} ({job.kind}) {job.status}{self._digest_tail(job)}")
        if not parts:
            return ""
        return (
            "[background jobs finished since your last turn "
            "(tail shown; full output via job_output):\n"
            + "\n".join(parts)
            + "]"
        )


def render_jobs(jobs: list[Job]) -> str:
    """The jobs panel body: one ``[glyph] id  kind  label`` line per job, with a
    trailing ``(exit/...)`` hint for finished jobs. Empty string when none."""
    lines = []
    for job in jobs:
        glyph = _GLYPH.get(job.status, "?")
        suffix = "" if job.status == "running" else f"  ({job.status})"
        lines.append(f"[{glyph}] {job.id}  {job.kind}  {job.label}{suffix}")
    return "\n".join(lines)
