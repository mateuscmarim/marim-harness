"""Desktop-notification dedup for finished background jobs.

Tracks which completed jobs have already been pinged so each finish notifies
exactly once. Kept separate from the wake *policy* (`wake.py`) — wake decides
whether to fire an autonomous turn, this decides whether to ping the desktop;
they never share state. Free of Textual and the notifier daemon so the dedup
decision is unit-testable on its own."""

from __future__ import annotations

from collections.abc import Iterable
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from marim_harness.jobs import Job


class FinishedJobNotifier:
    """Owns the set of job ids already desktop-notified. The App turns the
    returned list into actual notifications (the off-event-loop ``send``)."""

    def __init__(self) -> None:
        self.notified: set[str] = set()

    def newly_finished(self, jobs: Iterable[Job]) -> list[Job]:
        """Return each job that has genuinely completed (``done``/``failed``) and
        has not been notified yet, recording its id so a later poll skips it.
        Cancelled jobs are excluded — they're agent-initiated or shutdown
        teardown, so a ping would be noise (and ``cancel_all`` on exit stays
        silent)."""
        fresh: list[Job] = []
        for job in jobs:
            if job.status in ("done", "failed") and job.id not in self.notified:
                self.notified.add(job.id)
                fresh.append(job)
        return fresh
