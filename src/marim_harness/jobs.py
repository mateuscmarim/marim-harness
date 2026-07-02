"""Background jobs: a per-session, in-memory registry of detached work the agent
launches and later inspects.

Two kinds of work share one lifecycle — a shell process (``bash``) and an
isolated agent run (``agent``). The registry is agnostic to *how* either runs: it
wraps an awaitable that yields the final text, tracks status around it
(``running`` → ``done`` | ``failed`` | ``cancelled``), and knows how to stop it
(cancel the task, plus an optional ``kill`` for the OS process). Live output for
a running job comes from an optional ``output_fn`` (a bash job's growing buffer);
agent jobs have none and read ``(still running)`` until done.

State lives on :class:`~marim_harness.runtime.deps.Deps` next to the task checklist:
tools mutate it via ``ctx.deps.jobs``, and the TUI subscribes to ``on_change`` to
repaint a live panel. Nothing is persisted — jobs belong to the running process
and are cancelled on exit. The agent reaches results by *pulling*
(``job_output`` / ``wait_for_job``); nothing wakes a turn on its own.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import Literal

logger = logging.getLogger(__name__)

Status = Literal["running", "done", "failed", "cancelled"]


class PrerequisiteFailed(RuntimeError):
    """A dependent background job's prerequisite settled failed/cancelled (or
    vanished), so the dependent never started. Raised by the spawn wrapper and
    formatted by the registry's done-callback into the job's ``failed`` result."""


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
    result: str | None = None
    task: asyncio.Task | None = field(default=None, repr=False)
    kill: Callable[[], None] | None = field(default=None, repr=False)
    output_fn: Callable[[], str] | None = field(default=None, repr=False)


class JobRegistry:
    """The session's live background jobs. Mutated in place so the TUI's reference
    and ``on_change`` wiring survive across session switches."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0
        self.on_change = on_change
        # Ids of jobs that reached a terminal state since the digest was last
        # drained — surfaced to the model at the start of its next turn so a
        # fire-and-forget result is never silently forgotten.
        self._finished_since_turn: list[str] = []
        # Ids of jobs whose result was already consumed by wait_for_job during
        # the current turn — the wake scheduler skips these so a redundant
        # autonomous turn doesn't fire after the agent already got the result.
        self._wake_consumed: set[str] = set()

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _settle(self, job: Job, status: Status, result: str | None = None) -> None:
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
        kill: Callable[[], None] | None = None,
        output_fn: Callable[[], str] | None = None,
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

        def _on_done(t: asyncio.Task) -> None:
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

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def list(self) -> list[Job]:
        """Every job, in launch order."""
        return list(self._jobs.values())

    def output(self, job_id: str, *, mark_seen: bool = False) -> str:
        """The job's output: the final result once finished, or the live buffer
        (bash) / a running marker while it's still going.

        When ``mark_seen`` is set and the job has already finished, its id is
        marked wake-consumed so the autonomous wake scheduler won't fire a
        redundant turn — the caller (an agent tool) now has the result, exactly
        as :meth:`wait` does. Passive readers (the TUI jobs command) leave it
        unset so a job the agent hasn't reacted to still wakes a turn."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        if job.status == "running":
            if job.output_fn is not None:
                return job.output_fn() or "(running, no output yet)"
            return "(still running)"
        if mark_seen:
            self._wake_consumed.add(job_id)
        return job.result or ""

    async def wait(self, job_id: str, timeout: float = 60) -> str:
        """Block until the job finishes or ``timeout`` elapses, then return its
        result. A timeout leaves the job running (it isn't cancelled).

        When the job completes during the wait its id is marked as
        wake-consumed so the autonomous wake scheduler won't fire a redundant
        turn — the caller already has the result. The digest entry is preserved
        so the model still sees it at the start of its next turn."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        if job.status != "running" or job.task is None:
            # Already finished — mark as wake-consumed.
            self._wake_consumed.add(job_id)
            return job.result if job.result is not None else f"({job.status})"
        try:
            await asyncio.wait_for(asyncio.shield(job.task), timeout)
        except asyncio.TimeoutError:
            return f"job {job_id} still running after {timeout:g}s"
        except asyncio.CancelledError:
            pass  # the job itself was cancelled while we waited
        except Exception as exc:
            logger.debug("wait for job %s: %s (already settled)", job_id, exc)
        # Job finished (or was already settled) — mark as wake-consumed.
        self._wake_consumed.add(job_id)
        return job.result if job.result is not None else f"({job.status})"

    async def await_settled(self, ids: list[str]) -> list[Job]:
        """Block until every job in ``ids`` reaches a terminal state, then return
        their ``Job`` objects in the order the ids were given. No timeout — a
        dependent job legitimately waits as long as its prerequisites run.

        Each id is marked wake-consumed exactly as :meth:`wait` does: the waiter
        is the consumer, so an intermediate completion in a chain must not fire a
        redundant autonomous wake (digest entries are preserved, so the model
        still sees the full chain history next turn).

        Cancellation is two-sided and must not be conflated: a *dependency*
        being cancelled settles it and is returned like any terminal state (the
        caller decides what a cancelled prerequisite means), while the *waiter*
        being cancelled re-raises so the wrapper job itself settles cancelled.
        The shield makes the waiter's cancellation leave the dependency running.
        """
        settled: list[Job] = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None:
                # Spawn-time validation guarantees existence; a vanished id means
                # the registry was swapped/cleared out from under the chain.
                raise PrerequisiteFailed(f"prerequisite {jid} no longer exists")
            while job.status == "running":
                if job.task is None:
                    break  # registered but never scheduled; settle can't come
                try:
                    await asyncio.shield(job.task)
                except asyncio.CancelledError:
                    # Ambiguous by construction: shield raises CancelledError both
                    # when the dependency's task was cancelled and when *we* were.
                    # The dependency's task state disambiguates.
                    if not job.task.cancelled():
                        raise  # the waiter itself was cancelled — propagate
                except Exception:  # noqa: BLE001 — job failures settle via the
                    pass  # done-callback; status is read below, never the exc.
                # The done-callback that settles the job runs *after* the await
                # returns; yield once so status is terminal before we re-check
                # (otherwise this loop would spin on a done-but-unsettled task).
                await asyncio.sleep(0)
            self._wake_consumed.add(jid)
            settled.append(job)
        return settled

    async def cancel(self, job_id: str) -> str:
        """Stop a running job: kill its OS process if any, then cancel the task."""
        job = self._jobs.get(job_id)
        if job is None:
            return f"No job {job_id!r}."
        # The agent (or shutdown) is acting on this job, so mark it wake-consumed:
        # an agent-initiated cancel must not fire a redundant autonomous wake.
        # The digest still records the outcome for the model's next turn.
        self._wake_consumed.add(job_id)
        if job.status != "running":
            return f"job {job_id} already {job.status}"
        if job.kill is not None:
            job.kill()
        if job.task is not None:
            job.task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await job.task
        # A task cancelled before it began running never hits the wrapper's
        # except, so settle here; _settle is a no-op if it already landed.
        self._settle(job, "cancelled")
        return f"cancelled {job_id}"

    async def cancel_all(self) -> None:
        """Cancel every running job (called on shutdown)."""
        for job in list(self._jobs.values()):
            if job.status == "running":
                await self.cancel(job.id)

    def clear_history(self) -> None:
        """Drop terminal (done/failed/cancelled) jobs and the digest/wake buffers
        so the jobs panel and next-turn digest start as empty as a wiped
        conversation. Called by ``/clear``. Running jobs are *kept* — clearing the
        conversation shouldn't silently kill live background work — and their
        results will surface in a later digest when they finish."""
        self._jobs = {jid: job for jid, job in self._jobs.items() if job.status == "running"}
        # Drained buffers reference only settled jobs, all of which are now gone.
        self._finished_since_turn = []
        self._wake_consumed.clear()
        self._notify()

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

    def any_running(self) -> bool:
        """True if any job is still in the ``running`` state."""
        return any(j.status == "running" for j in self._jobs.values())

    def has_finished_pending(self) -> bool:
        """True if one or more jobs finished since the last
        :meth:`take_finished_digest` **and** were not already consumed by
        :meth:`wait`. Read-only — unlike ``take_finished_digest`` it does
        **not** drain the buffer, so the wake scheduler can decide whether
        to fire an autonomous turn without consuming the digest the turn needs."""
        return any(jid not in self._wake_consumed for jid in self._finished_since_turn)

    def take_finished_digest(self) -> str:
        """Summary of jobs that finished since this was last called, then clear the
        buffer. Empty string when nothing finished. Finished agent jobs inline their
        full result so the synthesis turn needs no extra ``job_output`` round-trips;
        bash jobs keep a tail of their output. The Harness prepends this to the next
        turn so the model notices completions it didn't wait on."""
        ids = self._finished_since_turn
        self._finished_since_turn = []
        self._wake_consumed.clear()
        parts = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None:
                continue
            if job.kind == "agent" and job.status == "done" and job.result:
                # Inline the whole report so the synthesis turn needs no extra
                # job_output round-trips. Size is conditionally bounded: the
                # auto-detach path defaults a budget so those reports are capped +
                # spilled before the result lands here; an explicit background=True
                # spawn with no max_output_chars is inlined in full.
                parts.append(
                    f"{job.id} ({job.kind}) {job.status} — full report:\n{job.result}"
                )
            else:
                parts.append(
                    f"{job.id} ({job.kind}) {job.status}{self._digest_tail(job)}"
                )
        if not parts:
            return ""
        return (
            "[background jobs finished since your last turn "
            "(agent reports inlined; bash tail shown, full output via job_output):\n"
            + "\n".join(parts)
            + "]"
        )


_JOBS_LABEL_WIDTH = 60


def _one_line(text: str, width: int = _JOBS_LABEL_WIDTH) -> str:
    """The first non-empty line of ``text`` (the meaningful summary; a verbose
    multi-section prompt's body is dropped), whitespace-collapsed and clipped to
    ``width`` with an ellipsis — so a label can never spill the jobs panel."""
    first = next((ln for ln in text.splitlines() if ln.strip()), "")
    s = " ".join(first.split())
    return s if len(s) <= width else s[: width - 1].rstrip() + "…"


def render_jobs(jobs: list[Job]) -> str:
    """The jobs panel body: one ``[glyph] id  col  title`` line per job, with a
    trailing ``(status)`` hint for finished jobs. Empty string when none.

    For an agent (sub-agent) job the ``col`` is the agent *type* (``explore`` /
    ``general`` / a custom name) — parsed from the ``"<type>: <title>"`` label —
    which is more informative than the bare ``agent`` kind; the title is the
    concise remainder, clipped to one line. Other jobs (bash) keep their kind."""
    lines = []
    for job in jobs:
        glyph = _GLYPH.get(job.status, "?")
        suffix = "" if job.status == "running" else f"  ({job.status})"
        if job.kind == "agent":
            col, sep, title = job.label.partition(": ")
            if not sep or not title.strip():  # no "<type>: " prefix — show as-is
                col, title = job.kind, job.label
        else:
            col, title = job.kind, job.label
        lines.append(f"[{glyph}] {job.id}  {col}  {_one_line(title)}{suffix}")
    return "\n".join(lines)
