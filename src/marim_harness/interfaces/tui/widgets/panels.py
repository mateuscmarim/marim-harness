"""Live panels pinned above the status bar: the agent's task checklist and the
session's background jobs. Each hides itself when empty so it takes no space."""

from textual import events
from textual.containers import VerticalScroll
from textual.content import Content
from textual.message import Message
from textual.widgets import Static


class PanelHeader(Static, can_focus=True):
    """Clickable header for collapsible panels. Follows CollapsibleTitle
    pattern: can_focus + pointer cursor + event.stop() so VerticalScroll
    doesn't consume the click for scrolling."""

    DEFAULT_CSS = """
    PanelHeader { pointer: pointer; }
    PanelHeader:hover { background: $panel; }
    """

    class Clicked(Message):
        """Posted when the header is clicked."""

    async def _on_click(self, event: events.Click) -> None:
        event.stop()
        self.post_message(self.Clicked())


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
    whenever there are no jobs. Title is sticky and toggles collapse on click."""

    def __init__(self) -> None:
        super().__init__(id="job-panel")
        self.display = False
        self._collapsed = False
        self._count = 0
        self._header = PanelHeader(id="job-header")
        self._body = Static(id="job-body")

    def compose(self):
        yield self._header
        yield self._body

    def on_panel_header_clicked(self, event: PanelHeader.Clicked) -> None:
        """Toggle collapse when the header is clicked."""
        self._collapsed = not self._collapsed
        self._body.display = not self._collapsed
        self._update_header()

    def _update_header(self) -> None:
        glyph = "\u25b8" if self._collapsed else "\u25be"
        self._header.update(
            Content.from_markup(
                f"[b $accent]{glyph} Jobs[/] [dim]({self._count})[/]"
            )
        )

    def show_jobs(self, jobs: list) -> None:
        """Render the current jobs, or hide the panel when there are none."""
        from ....jobs import render_jobs

        if not jobs:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        self._count = len(jobs)
        self._update_header()
        self._body.update(Content(render_jobs(jobs)))
