"""Live panels pinned above the status bar: the agent's task checklist and the
session's background jobs. Each hides itself when empty so it takes no space."""

from textual.content import Content
from textual.widgets import Static


class TaskPanel(Static):
    """The agent's live checklist, pinned above the status bar. Hidden whenever
    the list is empty so it takes no space when unused."""

    def __init__(self) -> None:
        super().__init__(id="task-panel")
        self.display = False

    def show_tasks(self, items: list) -> None:
        """Render the current checklist, or hide the panel when there are none."""
        from ....tasks import render_tasks

        if not items:
            self.display = False
            self.update("")
            return
        self.display = True
        # The header is intentional markup; the task body is untrusted and may
        # contain bracket sequences that escape() can't neutralise, so render it
        # as a literal Content appended to the parsed header.
        self.update(Content.from_markup("[b $accent]Tasks[/]\n") + Content(render_tasks(items)))


class JobPanel(Static):
    """The session's live background jobs, pinned above the status bar (a sibling
    of the task panel). Hidden whenever there are no jobs."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ....jobs import render_jobs

        if not jobs:
            self.display = False
            self.update("")
            return
        self.display = True
        # The header is intentional markup; the job labels are untrusted and may
        # contain bracket sequences that escape() can't neutralise, so render them
        # as a literal Content appended to the parsed header.
        self.update(Content.from_markup("[b $accent]Jobs[/]\n") + Content(render_jobs(jobs)))
