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
from dataclasses import dataclass, field
from typing import Awaitable, Callable, Optional

Status = str  # "running" | "done" | "failed" | "cancelled"

_GLYPH = {"running": "▸", "done": "+", "failed": "x", "cancelled": "x"}


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

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

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

        async def wrapper() -> None:
            try:
                job.result = await coro
                job.status = "done"
            except asyncio.CancelledError:
                job.status = "cancelled"
                raise
            except Exception as exc:  # a job failure never escapes into the loop
                job.result = f"{type(exc).__name__}: {exc}"
                job.status = "failed"
            finally:
                self._notify()

        job.task = asyncio.create_task(wrapper())
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
        # except, so set the terminal status here authoritatively.
        if job.status == "running":
            job.status = "cancelled"
            self._notify()
        return f"cancelled {job_id}"

    async def cancel_all(self) -> None:
        """Cancel every running job (called on shutdown)."""
        for job in list(self._jobs.values()):
            if job.status == "running":
                await self.cancel(job.id)


def render_jobs(jobs: list[Job]) -> str:
    """The jobs panel body: one ``[glyph] id  kind  label`` line per job, with a
    trailing ``(exit/...)`` hint for finished jobs. Empty string when none."""
    lines = []
    for job in jobs:
        glyph = _GLYPH.get(job.status, "?")
        suffix = "" if job.status == "running" else f"  ({job.status})"
        lines.append(f"[{glyph}] {job.id}  {job.kind}  {job.label}{suffix}")
    return "\n".join(lines)
