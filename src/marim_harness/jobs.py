"""Background jobs: a per-session, in-memory registry of detached work the agent
launches and later inspects.

Two kinds of work share one lifecycle — a shell process (``bash``) and an
isolated agent run (``agent``). The registry is agnostic to *how* either runs: it
wraps an awaitable that yields the final text, tracks status around it
(``running`` → ``done`` | ``failed`` | ``cancelled``), and knows how to stop it
(cancel the task, plus an optional ``kill`` for the OS process). Live output for
a running job comes from an optional ``output_fn`` (a bash job's growing buffer);
agent jobs have none and read ``(still running)`` until done.

CLI-native agents are observed rows: their backend owns execution and delivery
of results. They share the display/history lifecycle, but never participate in
native cancellation, dependencies, finished digests, or autonomous wake scheduling.

State lives on :class:`~marim_harness.runtime.deps.Deps` next to the task checklist:
tools mutate it via ``ctx.deps.jobs``, and the TUI subscribes to ``on_change`` to
repaint a live panel. Live jobs belong to the running process and are cancelled
on exit; settled summaries, though, are exported (:meth:`JobRegistry.export_settled`)
into the session payload and re-imported as read-only ``history`` on resume, so
the jobs panel and sub-agent cards survive a restart. The agent reaches results
by *pulling* (``job_output`` / ``wait_for_job``) or, between turns, through the
finished-job digest and the interfaces' autonomous wake.

A pull that blocks (:meth:`JobRegistry.wait`) is completion-based by default: it
returns when the job settles, with no periodic timeout round-trips through the
model. Two things end it early. An explicit ``timeout`` (kept for callers that
want a bounded block) returns a still-running note. And user steering: the turn
controller calls :meth:`JobRegistry.release_waits` the moment a steer is scheduled
for the model, so a wait that would otherwise hold the next model request back
indefinitely returns a truthful *released* outcome — the job keeps running, its
completion stays un-consumed (a later digest/wake still surfaces it), and the
model reads the guidance in the very request that carries the tool result.
"""

from __future__ import annotations

import asyncio
import logging
import re
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Literal, Protocol

logger = logging.getLogger(__name__)

Status = Literal["running", "done", "failed", "cancelled"]


class PrerequisiteFailed(RuntimeError):
    """A dependent background job's prerequisite settled failed/cancelled (or
    vanished), so the dependent never started. Raised by the spawn wrapper and
    formatted by the registry's done-callback into the job's ``failed`` result."""


_GLYPH = {"running": "▸", "done": "+", "failed": "x", "cancelled": "x"}

# The terminal statuses a persisted history entry can carry — "running" is
# deliberately excluded (a history row is by definition settled).
_SETTLED_STATUSES: frozenset[Status] = frozenset({"done", "failed", "cancelled"})


def _validated_status(raw: object) -> Status:
    """Coerce an imported history entry's ``status`` to a known :data:`Status`,
    falling back to ``"done"`` for anything unrecognized (a forward-compat
    guard against a session file written by a newer/older version)."""
    return raw if raw in _SETTLED_STATUSES else "done"


# How many trailing chars of a finished job's output to inline in the next-turn
# digest. The tail carries the verdict (a test summary, a final error), so a
# short tail lets the model read the result without a separate job_output pull,
# while the cap keeps the prompt from ballooning when many jobs finish at once.
_DIGEST_RESULT_CHARS = 200

# How many settled-job summaries a session payload carries at most (see
# JobRegistry.export_settled) — a long-lived session shouldn't accrete an
# unbounded history.
_HISTORY_CAP = 50


def result_tail(result: str | None) -> str:
    """The last _DIGEST_RESULT_CHARS chars of a result, whitespace-collapsed —
    the same verdict-carrying tail the digest inlines, the persisted history
    stores, and the jobs list DTO carries over the wire. Idempotent: the tail
    of a tail is that tail."""
    if not result:
        return ""
    compact = " ".join(result.split())
    if len(compact) > _DIGEST_RESULT_CHARS:
        compact = "…" + compact[-_DIGEST_RESULT_CHARS:]
    return compact


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
    # The spawn's tool_call_id when kind == "agent" — the cross-cutting key that
    # joins a settled job back to its sub-agent card and transcript sidecar.
    stream_id: str | None = None
    # UTC ISO stamp set at settle time; rides into the persisted history.
    finished_at: str | None = None
    # UTC ISO stamp set at register time (running jobs only; not persisted).
    started_at: str | None = None
    # The spawn input: the sub-agent task prompt (agent) or the command (bash).
    prompt: str | None = None
    # Observation only: the CLI backend, not this registry, controls execution.
    backend_owned: bool = False
    task: asyncio.Task | None = field(default=None, repr=False)
    kill: Callable[[], None] | None = field(default=None, repr=False)
    output_fn: Callable[[], str] | None = field(default=None, repr=False)


WaitKind = Literal["settled", "timeout", "released", "missing", "backend"]


@dataclass(frozen=True)
class WaitOutcome:
    """How a :meth:`JobRegistry.wait_outcome` call ended, and the text the
    model-facing wait tools return for it.

    ``settled`` — the job reached a terminal state and ``text`` is its result
    (or ``(<status>)`` for a result-less cancel); this is the only kind that
    marks the job wake-consumed. ``timeout`` — an explicit timeout elapsed.
    ``released`` — user steering arrived and released the wait so the model can
    read the guidance; the job keeps running. ``missing`` / ``backend`` — no
    such job, or a CLI-owned job whose waiting the backend manages. ``elapsed``
    is how long the call actually blocked, in seconds — what a UI should show,
    rather than any requested timeout."""

    kind: WaitKind
    text: str
    elapsed: float = 0.0


def history_rows(entries: list[dict]) -> list[Job]:
    """Persisted settled summaries (the ``jobs`` list a session file carries,
    see :meth:`JobRegistry.export_settled`) as read-only :class:`Job` rows —
    no task, no kill, ``result`` is the stored tail. Shared by the registry's
    ``import_history`` and the daemon's cold-session jobs listing, so both
    read a session file's history the same way."""
    return [
        Job(
            id=str(e.get("id", "?")),
            kind=str(e.get("kind", "agent")),
            label=str(e.get("label", "")),
            status=_validated_status(e.get("status")),
            result=e.get("result_tail") or None,
            stream_id=e.get("stream_id"),
            finished_at=e.get("finished_at"),
            prompt=e.get("prompt"),
            backend_owned=e.get("backend_owned") is True,
        )
        for e in entries
        if isinstance(e, dict)
    ]


class JobsView(Protocol):
    """The read surface the TUI's panels and replay need from "the session's
    jobs": the live :class:`JobRegistry` in process, a read-only mirror of the
    daemon's registry when attached (``interfaces/tui/remote_jobs.py``). The
    mutating verbs (cancel, resume) are link commands, not part of this."""

    @property
    def history(self) -> list[Job]: ...
    def list(self) -> list[Job]: ...
    def get(self, job_id: str) -> Job | None: ...
    def any_running(self) -> bool: ...
    def has_finished_pending(self) -> bool: ...


class JobRegistry:
    """The session's live background jobs. Mutated in place so the TUI's reference
    and ``on_change`` wiring survive across session switches."""

    def __init__(self, on_change: Callable[[], None] | None = None) -> None:
        self._jobs: dict[str, Job] = {}
        self._counter = 0
        # A successful CLI conversation release invalidates every old observer,
        # even one that had not seen its first spawn before the session switch.
        self.observation_epoch: int = 0
        self.on_change = on_change
        # Ids of jobs that reached a terminal state since the digest was last
        # drained — surfaced to the model at the start of its next turn so a
        # fire-and-forget result is never silently forgotten.
        self._finished_since_turn: list[str] = []
        # Ids of jobs whose result was already consumed by wait_for_job during
        # the current turn — the wake scheduler skips these so a redundant
        # autonomous turn doesn't fire after the agent already got the result.
        self._wake_consumed: set[str] = set()
        # Poll ledger: consecutive identical read-only observations per surface
        # ("list", "output:<job-id>") since the last state change. Read by the
        # jobs tools (via note_poll) to nudge a model out of busy-polling with
        # an escalating no-change response; any register/settle/clear resets it
        # because the next poll genuinely has something new to see. Deliberately
        # NOT reset at turn boundaries — the ledger keys off job state, not
        # turns (spec 2026-07-02-job-poll-guard-design).
        self._poll_ledger: dict[str, tuple[str, int]] = {}
        # Wait release. Every pending wait() parks on the current release future
        # alongside its job's task; release_waits() resolves it — waking all of
        # them at once — and the next waiter creates a fresh one. Lazily built
        # because a Future needs a running loop; the count is what
        # release_waits() reports.
        self._wait_release: asyncio.Future[None] | None = None
        self._parked_waits = 0
        # Level-triggered companion to release_waits(): whether user guidance is
        # scheduled for the model but not yet delivered. release_waits() only
        # wakes waits that already exist; a wait that STARTS after the steer
        # was scheduled (the steer landed while the model was still composing
        # the response that calls wait_for_job) must not block either, or the
        # guidance would sit behind it until the job finishes. The turn
        # controller binds this to its delivery-receipt check; the default
        # never releases, so embedders without steering see a plain wait.
        self.guidance_pending: Callable[[], bool] = lambda: False
        # Settled-job summaries imported from the persisted session (spec
        # 2026-07-03-subagent-resume, §2). Read-only display state: never in
        # ``_jobs``, never killable/pollable, never in the digest — a prior
        # process already surfaced these results.
        self.history: list[Job] = []

    def _notify(self) -> None:
        if self.on_change is not None:
            self.on_change()

    def _settle(self, job: Job, status: Status, result: str | None = None) -> None:
        """Move a running job to its terminal ``status`` exactly once: record it
        for the next-turn digest and repaint. A no-op if already terminal, so the
        wrapper's cancel path and an explicit ``cancel()`` can't double-count."""
        if job.status != "running":
            return
        job.finished_at = datetime.now(timezone.utc).isoformat()
        job.status = status
        self._poll_ledger.clear()
        if result is not None:
            job.result = result
        if not job.backend_owned:
            self._finished_since_turn.append(job.id)
        self._notify()

    def _next_id(self) -> str:
        self._counter += 1
        return f"job-{self._counter}"

    def note_poll(self, key: str, snapshot: str) -> int:
        """Record one read-only poll of ``key`` (a tool surface: ``"list"`` or
        ``"output:<job-id>"``) that observed ``snapshot``, and return how many
        consecutive polls of that key saw this exact snapshot (1 = first sight,
        or changed since last time). Snapshots must be stable projections —
        never include elapsed-time renderings, or the count can never rise."""
        last, count = self._poll_ledger.get(key, ("", 0))
        count = count + 1 if snapshot == last else 1
        self._poll_ledger[key] = (snapshot, count)
        return count

    def register(
        self,
        kind: str,
        label: str,
        coro: Awaitable[str],
        *,
        kill: Callable[[], None] | None = None,
        output_fn: Callable[[], str] | None = None,
        stream_id: str | None = None,
        prompt: str | None = None,
    ) -> str:
        """Schedule ``coro`` as a background job and return its id. The coroutine's
        return value becomes the job's result; an exception marks it failed; being
        cancelled marks it cancelled. Fires ``on_change`` on launch and finish."""
        job = Job(
            id=self._next_id(),
            kind=kind,
            label=label,
            kill=kill,
            output_fn=output_fn,
            stream_id=stream_id,
            prompt=prompt,
            started_at=datetime.now(timezone.utc).isoformat(),
        )

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
        self._poll_ledger.clear()
        self._notify()
        return job.id

    def get(self, job_id: str) -> Job | None:
        return self._jobs.get(job_id)

    def observe_agent(self, stream_id: str, label: str, prompt: str | None = None) -> Job:
        """Publish a CLI-owned agent without scheduling a native task.

        Each call creates a row; the transport observer owns backend identity
        mapping and calls reopen_observed for subsequent runs of the same agent.
        """
        job = Job(
            id=self._next_id(),
            kind="agent",
            label=label,
            stream_id=stream_id,
            prompt=prompt,
            backend_owned=True,
            started_at=datetime.now(timezone.utc).isoformat(),
        )
        self._jobs[job.id] = job
        self._poll_ledger.clear()
        logger.debug("observed CLI agent registered: job=%s", job.id)
        self._notify()
        return job

    def _validate_observed(self, job: Job) -> None:
        if not job.backend_owned or self._jobs.get(job.id) is not job:
            raise ValueError("job is not an observed agent in this registry")

    def discard_observed(self) -> None:
        """Invalidate old CLI observers and remove their in-process rows.

        Session switches reuse this registry and may already have imported the
        incoming session's history. Leave that history and native work intact;
        the releasing transport still owns stopping its old backend agents.
        """
        self.observation_epoch += 1
        remaining = {jid: job for jid, job in self._jobs.items() if not job.backend_owned}
        removed = len(self._jobs) - len(remaining)
        logger.debug(
            "CLI job observations invalidated: epoch=%s removed=%s",
            self.observation_epoch,
            removed,
        )
        if not removed:
            return
        self._jobs = remaining
        self._poll_ledger.clear()
        self._notify()

    def settle_observed(self, job: Job, status: Status, result: str) -> None:
        """Record a backend's terminal outcome once, without native result delivery."""
        self._validate_observed(job)
        if status not in _SETTLED_STATUSES:
            raise ValueError("observed job must settle to a terminal status")
        if job.status != "running":
            return
        logger.debug("observed CLI agent settled: job=%s status=%s", job.id, status)
        self._settle(job, status, result)

    def reopen_observed(self, job: Job) -> None:
        """Start another backend run under the same job and stream identity."""
        self._validate_observed(job)
        if job.status == "running":
            return
        job.status = "running"
        job.result = None
        job.finished_at = None
        job.started_at = datetime.now(timezone.utc).isoformat()
        self._poll_ledger.clear()
        logger.debug("observed CLI agent reopened: job=%s", job.id)
        self._notify()

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

    async def wait(self, job_id: str, timeout: float | None = None) -> str:
        """Block until the job finishes, then return its result — the text of
        :meth:`wait_outcome`, for callers that only need the message."""
        return (await self.wait_outcome(job_id, timeout)).text

    async def wait_outcome(self, job_id: str, timeout: float | None = None) -> WaitOutcome:
        """Block until the job finishes and return a :class:`WaitOutcome`.

        Completion-based by default (``timeout=None``): the call returns when
        the job settles, however long that takes. An explicit ``timeout`` is
        honoured for callers that want a bounded block; on expiry the job is
        left running (it isn't cancelled) and the outcome is ``timeout``.

        User steering ends a wait early: :meth:`release_waits` (called by the
        turn controller when a steer is scheduled for the model) wakes every
        parked wait with a ``released`` outcome, and a wait that starts while
        :attr:`guidance_pending` reads true returns ``released`` at once. Both
        leave the job running and un-consumed. A job that settles in the same
        loop step as a release is reported ``settled`` — the result exists, so
        it is delivered rather than withheld.

        Cancellation is two-sided and must not be conflated: the job's own task
        being cancelled settles it and is returned like any terminal state (the
        caller decides what a cancelled job means), while the *waiter* being
        cancelled re-raises so the caller's own task settles cancelled. The
        shield in :meth:`_park` makes the waiter's cancellation leave the job
        running.

        Only a ``settled`` outcome marks the job wake-consumed: the caller then
        holds the result, so the autonomous wake scheduler must not fire a
        redundant turn for it. A released, timed-out, or cancelled wait never
        delivered anything, so the completion stays pending for a later
        digest/wake. The digest entry is preserved either way, so the model
        still sees it at the start of its next turn."""
        job = self._jobs.get(job_id)
        if job is None:
            return WaitOutcome("missing", f"No job {job_id!r}.")
        if job.backend_owned and job.status == "running":
            return WaitOutcome(
                "backend", f"job {job_id} still running; waiting is managed by the CLI backend"
            )
        if job.status != "running" or job.task is None:
            # Already finished — mark as wake-consumed.
            self._wake_consumed.add(job_id)
            return WaitOutcome("settled", self._settled_text(job))
        if self.guidance_pending():
            return WaitOutcome("released", self._released_text(job_id, 0.0))
        t0 = time.monotonic()
        released = await self._park(job, timeout)
        elapsed = time.monotonic() - t0
        if not job.task.done():
            return self._unfinished_outcome(job_id, released, timeout, elapsed)
        # The done-callback that settles the job runs *after* the task completes
        # — the wake from asyncio.wait may land before it. Yield until the
        # status is terminal before reading it, mirroring the recheck loop in
        # :meth:`await_settled`.
        while job.status == "running":
            await asyncio.sleep(0)
        # Job finished — the caller gets the result, so mark it wake-consumed.
        self._wake_consumed.add(job_id)
        return WaitOutcome("settled", self._settled_text(job), elapsed)

    async def _park(self, job: Job, timeout: float | None) -> bool:
        """Block on ``job``'s task or a wait release, whichever comes first (or
        ``timeout``). Returns whether a release fired; the caller reads the
        task's state to tell completion from a timeout.

        The task is shielded so the waiter's own cancellation (a user abort
        while the model sits in wait_for_job) propagates out of here as a plain
        CancelledError without touching the job — ``asyncio.wait`` never
        cancels what it waits on, and cancelling the shield's outer future on
        the way out only detaches us. The job's OWN cancellation raises
        nothing: it just shows up as a settled task in the done set, which is
        the disambiguation the old ``wait_for(shield(...))`` form had to infer
        from the task state inside an ``except``."""
        assert job.task is not None
        shielded = asyncio.shield(job.task)
        release = self._release_future()
        self._parked_waits += 1
        try:
            await asyncio.wait(
                {shielded, release}, timeout=timeout, return_when=asyncio.FIRST_COMPLETED
            )
        finally:
            self._parked_waits -= 1
            if not shielded.done():
                shielded.cancel()  # detach from the still-running task
            elif not shielded.cancelled():
                # A failed job settles via its done-callback; retrieve the
                # exception here so asyncio doesn't log it as never retrieved.
                shielded.exception()
        return release.done()

    def _release_future(self) -> asyncio.Future[None]:
        if self._wait_release is None or self._wait_release.done():
            self._wait_release = asyncio.get_running_loop().create_future()
        return self._wait_release

    def release_waits(self) -> int:
        """Wake every parked :meth:`wait` now with a ``released`` outcome and
        return how many there were. Called by the turn controller the moment a
        user steer is scheduled for the model: a completion-based wait would
        otherwise hold the next model request — and the guidance riding on it —
        back until the job finished. Jobs keep running; nothing is consumed. A
        no-op (0) when nothing is parked, so callers need not check first."""
        fut, self._wait_release = self._wait_release, None
        if fut is None or fut.done():
            return 0
        released = self._parked_waits
        fut.set_result(None)
        return released

    @staticmethod
    def _settled_text(job: Job) -> str:
        return job.result if job.result is not None else f"({job.status})"

    @staticmethod
    def _released_text(job_id: str, elapsed: float) -> str:
        return (
            f"job {job_id} still running — wait released after {elapsed:.0f}s "
            "because new user guidance arrived"
        )

    @classmethod
    def _unfinished_outcome(
        cls, job_id: str, released: bool, timeout: float | None, elapsed: float
    ) -> WaitOutcome:
        """The outcome for a wait that ended with the job still running: a
        release wins over a timeout (both can be true when the release lands
        as the deadline expires — the guidance is the more useful thing to
        report, and the job is still running either way)."""
        if released:
            return WaitOutcome("released", cls._released_text(job_id, elapsed), elapsed)
        limit = f"{timeout:g}s" if timeout is not None else "the wait"
        return WaitOutcome("timeout", f"job {job_id} still running after {limit}", elapsed)

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

        Every id is resolved to its ``Job`` object *before* waiting on any of
        them — two passes, not one lazy lookup per iteration. A chain step
        (``after=[A, B]``) can block arbitrarily long on an earlier id; if a
        later id (already finished, say B) were looked up only once the loop
        reached it, an intervening ``/clear`` (:meth:`clear_history` prunes
        terminal jobs out of ``self._jobs``) while still waiting on A would drop
        B from the registry, turning a legitimate held reference into a
        spurious "prerequisite no longer exists". Resolving up front means the
        held ``Job`` objects survive a concurrent ``clear_history`` regardless
        of how long the wait takes — the ``after=`` promise is to the objects,
        not to a lookup repeated over the course of the wait.
        """
        jobs: list[Job] = []
        for jid in ids:
            job = self._jobs.get(jid)
            if job is None:
                # Spawn-time validation guarantees existence; a vanished id means
                # the registry was swapped/cleared out from under the chain.
                raise PrerequisiteFailed(f"prerequisite {jid} no longer exists")
            if job.backend_owned and job.status == "running":
                raise PrerequisiteFailed(f"prerequisite {jid} is managed by the CLI backend")
            jobs.append(job)

        settled: list[Job] = []
        for jid, job in zip(ids, jobs, strict=True):
            while job.status == "running":
                if job.task is None:
                    # An observed prerequisite can reopen while we await an
                    # earlier native job. Recheck here rather than treating its
                    # absent native task as evidence that it finished.
                    raise PrerequisiteFailed(f"prerequisite {jid} has no registry-owned task")
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
        if job.backend_owned:
            logger.debug("observed CLI agent cancellation refused: job=%s", job_id)
            return f"job {job_id} is managed by the CLI backend; cannot cancel it individually"
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
            try:
                # shield, mirroring wait()/await_settled(): awaiting job.task
                # BARE would let the *caller* (the agent's cancel-job tool) being
                # cancelled by a turn abort propagate INTO job.task — Task.cancel
                # cancels the future its awaiter is blocked on — so job.task would
                # read cancelled either way and the disambiguation below couldn't
                # tell the two apart. The shield keeps our own cancellation off
                # job.task (which we already cancelled explicitly above), so its
                # state cleanly distinguishes the two cases.
                await asyncio.shield(job.task)
            except asyncio.CancelledError:
                # Ambiguous by construction: CancelledError arrives both when the
                # job task we cancelled finishes and when *we* were cancelled by
                # a turn abort mid-await. The job task's own state disambiguates:
                # if it isn't done-and-cancelled, the CancelledError is ours and
                # must propagate — cancellation delivery is one-shot, so
                # swallowing it here would let the turn keep running one more
                # step with the abort silently lost.
                if not job.task.cancelled():
                    raise
            except Exception as exc:
                logger.debug("cancel job %s: %s (already settled)", job_id, exc, exc_info=True)
        # A task cancelled before it began running never hits the wrapper's
        # except, so settle here; _settle is a no-op if it already landed.
        self._settle(job, "cancelled")
        return f"cancelled {job_id}"

    async def cancel_all(self) -> None:
        """Cancel every registry-owned running job (called on shutdown).

        Observed agents are stopped and settled by their owning CLI transport.

        Iterates in *reverse* launch order — this matters. A dependent job
        (``after=``) always registers after its prerequisite, so forward order
        cancels the prerequisite first; the dependent's ``await_settled`` then
        observes the prerequisite as ``cancelled`` and raises
        ``PrerequisiteFailed``, which settles the dependent ``failed`` via the
        ordinary done-callback path — *not* via this method's own ``cancel()``
        call, so it's never marked wake-consumed. A ``failed`` job with a
        pending (unconsumed) digest entry is exactly what
        ``has_finished_pending()`` looks for, so teardown could fire a
        redundant autonomous wake turn (and a desktop notification) for a job
        that only "failed" because we were shutting down. Cancelling in
        reverse order visits each dependent while its prerequisites are still
        running: cancelling the dependent's own task directly settles it
        ``cancelled`` and wake-consumed (via this method's ``cancel()`` call)
        *before* its prerequisites are ever touched, so the race can't occur.
        """
        for job in reversed(list(self._jobs.values())):
            if job.status == "running" and not job.backend_owned:
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
        self._poll_ledger.clear()
        self.history = []
        self._notify()

    def _digest_tail(self, job: Job) -> str:
        """A ``: <tail>`` snippet for a finished job's digest line — the last
        :data:`_DIGEST_RESULT_CHARS` chars of its result, whitespace-collapsed so
        the verdict reads on one line. Empty when the job has no result (e.g.
        cancelled)."""
        tail = result_tail(job.result)
        return f": {tail}" if tail else ""

    def export_settled(self) -> list[dict]:
        """Summaries of every terminal job — prior-session history first, then
        this process's settles — capped to the newest _HISTORY_CAP so a
        long-lived session doesn't accrete unboundedly. Results are persisted as
        tails, not full reports: the session payload must not balloon (full
        reports were already delivered via the digest or spill files)."""

        def entry(j: Job) -> dict:
            return {
                "id": j.id,
                "kind": j.kind,
                "label": j.label,
                "status": j.status,
                "result_tail": result_tail(j.result),
                "stream_id": j.stream_id,
                "finished_at": j.finished_at,
                "prompt": j.prompt,
                "backend_owned": j.backend_owned,
            }

        settled = [entry(j) for j in self._jobs.values() if j.status != "running"]
        prior = [entry(j) for j in self.history]
        return (prior + settled)[-_HISTORY_CAP:]

    def import_history(self, entries: list[dict]) -> None:
        """Load prior-session settled summaries as read-only ``history``. Also
        seeds the id counter past any imported ``job-N`` so a job launched this
        process never shares an id with a history row on the panel."""
        self.history = history_rows(entries)
        for job in self.history:
            m = re.fullmatch(r"job-(\d+)", job.id)
            if m:
                self._counter = max(self._counter, int(m.group(1)))
        self._notify()

    def any_running(self) -> bool:
        """Whether native work is running, for the all-jobs-settled wake gate.

        CLI backends deliver their own agent results; observed work must neither
        trigger a duplicate native wake nor block unrelated native completions.
        Display consumers should inspect list() for all running rows.
        """
        return any(j.status == "running" and not j.backend_owned for j in self._jobs.values())

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
                parts.append(f"{job.id} ({job.kind}) {job.status} — full report:\n{job.result}")
            else:
                parts.append(f"{job.id} ({job.kind}) {job.status}{self._digest_tail(job)}")
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
