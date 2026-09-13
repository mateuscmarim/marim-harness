"""The daemon's jobs, mirrored (phase 4b).

An attached TUI has no registry of its own: the jobs live on the daemon's
``JobRegistry``, and ``GET .../jobs`` is the authority. ``JobMirror`` holds
the last read as read-only ``Job`` rows behind the read surface every panel
and settle path already uses (``jobs.JobsView``), so the jobs panel, the
detached sub-agent cards and the replay settle run the same code they run
in process. The app re-reads on every ``jobs.changed`` (``HarnessApp.
refresh_remote_jobs``); nothing here talks to the wire.

Two things are deliberately NOT mirrored:

- ``has_finished_pending`` is always False. The daemon owns autonomous wake
  (its host is built with ``autonomous_wake=True``); a second driver in the
  attached process would race it.
- ``history`` is empty. The daemon's list already carries the persisted
  history rows (settled, after the running ones), so every row is in
  ``list()`` and the panel's ``history + list()`` paint shows each once.
"""

from __future__ import annotations

from ...jobs import Job


class JobMirror:
    """The daemon's registry as of the last ``GET jobs``."""

    def __init__(self) -> None:
        self._rows: dict[str, Job] = {}
        # Full results fetched with GET jobs/{id} for jobs whose card was
        # still pending when they settled. Kept across ``apply`` so a later
        # list read (which only carries tails) never downgrades a card
        # already filled with the full text, and never re-fetches it.
        self._full: dict[str, str] = {}
        # True once a read succeeded. Until then the replay cannot tell a
        # spawn the daemon is driving from one that died with it, and keeps
        # the 4a "unknown" behaviour (see SessionView._unknown_running).
        self.synced = False

    @property
    def history(self) -> list[Job]:
        return []

    def apply(self, rows: list[Job]) -> None:
        """Replace the snapshot with the daemon's rows, in the daemon's order."""
        for row in rows:
            full = self._full.get(row.id)
            if full is not None and row.status != "running":
                row.result = full
        self._rows = {row.id: row for row in rows}
        self.synced = True

    def list(self) -> list[Job]:
        return list(self._rows.values())

    def get(self, job_id: str) -> Job | None:
        return self._rows.get(job_id)

    def any_running(self) -> bool:
        return any(row.status == "running" for row in self._rows.values())

    def has_finished_pending(self) -> bool:
        return False

    def settled_needing_result(self, mapped_ids: set[str]) -> list[str]:
        """Settled jobs among ``mapped_ids`` (the detached cards still waiting
        on a result) whose full result has not been fetched yet."""
        return [
            jid
            for jid, row in self._rows.items()
            if jid in mapped_ids and row.status != "running" and jid not in self._full
        ]

    def set_result(self, job_id: str, result: str) -> None:
        """Record a job's full result (from ``GET jobs/{id}``)."""
        self._full[job_id] = result
        row = self._rows.get(job_id)
        if row is not None:
            row.result = result
