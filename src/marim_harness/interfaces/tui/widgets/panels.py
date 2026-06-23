"""Live panels pinned above the status bar: the agent's task checklist and the
session's background jobs. Each hides itself when empty so it takes no space."""

from textual.content import Content
from textual.containers import VerticalScroll
from textual.widgets import Collapsible, Static


class TaskPanel(VerticalScroll):
    """The agent's live checklist, pinned above the status bar. Hidden whenever
    the list is empty so it takes no space when unused."""

    def __init__(self) -> None:
        super().__init__(id="task-panel")
        self.display = False
        self._header = Static(id="task-header")
        self._body = Static(id="task-body")

    def compose(self):
        yield self._header
        yield self._body

    def show_tasks(self, items: list) -> None:
        """Render the current checklist, or hide the panel when there are none."""
        from ....tasks import render_tasks

        if not items:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        count = len(items)
        self._header.update(
            Content.from_markup(f"[b $accent]Tasks[/] [dim]({count})[/]")
        )
        self._body.update(Content(render_tasks(items)))


class JobPanel(VerticalScroll):
    """The session's live background jobs, pinned above the status bar. Hidden
    whenever there are no jobs. Collapsible and expanded by default."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False
        self._body = Static(id="job-body")
        self._collapsible = Collapsible(
            self._body,
            title=Content.from_markup("[b $accent]Jobs[/]"),
            id="job-collapsible",
            collapsed=False,
        )

    def compose(self):
        yield self._collapsible

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ....jobs import render_jobs

        if not jobs:
            self.display = False
            self._body.update("")
            return
        self.display = True
        count = len(jobs)
        self._collapsible.title = (
            Content.from_markup(f"[b $accent]Jobs[/] [dim]({count})[/]")
        )
        self._body.update(Content(render_jobs(jobs)))
