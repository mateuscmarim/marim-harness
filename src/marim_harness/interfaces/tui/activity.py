"""Everything that happens to the TUI from *outside* the turn: the task
checklist and background-job panels, desktop notifications, and the autonomous
wake that turns a finished job into a turn of its own.

They live together because they share one trigger — a job registry callback
firing on the app's event loop — and one hazard: each is invoked from callbacks
that may arrive before mount or during teardown, so every entry point here
guards on the app still being live rather than assuming a widget tree.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from ...runtime.wake import WakeController
from ...runtime.wake_driver import WakeDriver
from .notify import FinishedJobNotifier
from .widgets import JobPanel, TaskPanel

if TYPE_CHECKING:
    from .app import HarnessApp


class ActivityMonitor:
    """Owns the panels' repaint, the desktop-notification dedup, and the wake
    chain. The App keeps the ``autonomous_wake`` toggle itself (the user flips
    it from /jobs and from settings) — this reads it per decision."""

    def __init__(self, app: HarnessApp) -> None:
        self._app = app
        # Bounds the wake→spawn→wake chain and owns the should-wake decision; the
        # App keeps the public autonomous_wake toggle and the wake's side effects.
        self.wake = WakeDriver(
            WakeController(app.harness.wake_depth_cap),
            is_enabled=lambda: app.autonomous_wake,
            turn_busy=lambda: app.turn_busy,
            has_finished_pending=app.jobs.has_finished_pending,
            all_jobs_settled=lambda: not app.jobs.any_running(),
            enqueue_digest_turn=app.mount_wake_turn,
        )
        # Dedup tracker: pings each finished job exactly once, independent of
        # the autonomous-wake path.
        self._job_notifier = FinishedJobNotifier()

    def render_tasks(self) -> None:
        """Repaint the task panel from the harness's current checklist, plus a
        compact plan title when a plan has been presented this session."""
        try:
            panel = self._app.query_one(TaskPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        plan = self._app.harness.deps.plan
        panel.show_tasks(
            self._app.harness.deps.tasks.items,
            plan_title=plan.summary if plan is not None else None,
        )

    def on_tasks_changed(self) -> None:
        """Live callback from the update_tasks tool — repaint as the agent edits
        the list mid-turn. Fired on the app's event loop, so it's safe to touch
        widgets directly."""
        self.render_tasks()

    def render_jobs(self) -> None:
        """Repaint the jobs panel from the registry's current jobs, prior-session
        history first (history rows are terminal, so render_jobs already
        suffixes them ``(done)``/``(failed)``)."""
        if not self._app.is_running:
            return  # a job changed before mount / after teardown — on_mount paints
        try:
            panel = self._app.query_one(JobPanel)
        except NoMatches:
            return  # tearing down; nothing to paint
        jobs = self._app.jobs
        panel.show_jobs(jobs.history + jobs.list())

    def on_jobs_changed(self) -> None:
        """Live callback from the job registry — repaint as jobs launch and
        finish. Each job runs as a task on the app's event loop, so the callback
        fires there and direct widget mutation is safe."""
        self._app.stream.fill_finished_detached_cards(self._app.jobs)
        self.render_jobs()
        self.notify_finished_jobs()
        self.maybe_wake()

    def desktop_notify(self, title: str, body: str, event_type: str) -> None:
        """Fire a desktop notification if one is wired on deps. Best-effort —
        the notifier itself swallows all errors, so this is a safe no-op when
        notifications are off or the platform lacks a daemon.

        Dispatched OFF the event loop: the platform notifiers shell out and wait
        (the Windows balloon-tip backend alone sleeps ~5.5s), so calling the
        blocking ``send`` here — from turn-end / approval / job-completion
        callbacks — would freeze the whole UI. We schedule the async send path,
        which spawns the subprocess via asyncio and awaits it without blocking
        other tasks. Failures stay swallowed inside the notifier."""
        notifier = self._app.harness.deps.ui.notifier
        if notifier is not None:
            self._app.run_worker(
                notifier.send_async(title, body, event_type),
                name=f"notify:{event_type}",
                group="notifications",
                exit_on_error=False,
            )

    def notify_finished_jobs(self) -> None:
        """Desktop-notify once per genuinely completed (done/failed) background
        job. Decoupled from the autonomous-wake path so a completion still pings
        when wake is off, a turn is busy, or the depth cap is hit. Cancelled jobs
        are skipped — they're either agent-initiated or shutdown teardown, so a
        ping would be noise (and this keeps ``cancel_all`` on exit silent)."""
        for job in self._job_notifier.newly_finished(self._app.jobs.list()):
            self.desktop_notify(
                "Background job finished",
                f"{job.id} ({job.kind}) {job.status}",
                "job_done",
            )

    def maybe_wake(self) -> None:
        """Fire one digest-only autonomous turn iff a background job finished and
        nothing is blocking. The decision + depth bookkeeping live in the shared
        WakeDriver; this method only supplies the is-running mount guard."""
        if not self._app.is_running:
            return  # firing during teardown would race the unmount
        self.wake.maybe_wake()

    def note_user_turn(self) -> None:
        """Reset the wake chain — a turn the user started is not part of it."""
        self.wake.note_user_turn()
