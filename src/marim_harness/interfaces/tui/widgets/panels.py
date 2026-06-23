"""Live panels pinned above the status bar: the agent's task checklist and the
session's background jobs. Both share one widget — :class:`LivePanel` — which
hides itself when empty (so it takes no space unused) and collapses to its
title row on click."""

from collections.abc import Callable

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


class LivePanel(VerticalScroll):
    """A live, collapsible panel pinned above the status bar: a sticky title
    above a scrollable body. Hidden whenever it has no items, so it takes no
    space when unused; click the title to collapse the body to a single line.

    Subclasses supply a ``name`` (drives the ``{name}-panel/-header/-body``
    ids), a ``title``, and a ``renderer`` that turns the item list into text."""

    DEFAULT_CSS = """
    LivePanel {
        height: auto; max-height: 8; background: $panel; color: $text;
        padding: 0 1; border-top: tall $background;
    }
    LivePanel .live-panel-header {
        height: 1; dock: top; padding: 0;
        color: $accent; text-style: bold; content-align: left middle;
    }
    LivePanel .live-panel-body { height: auto; }
    """

    def __init__(self, *, name: str, title: str, renderer: Callable[[list], str],
                 markup: bool = False) -> None:
        super().__init__(id=f"{name}-panel")
        self.display = False
        self._title = title
        self._renderer = renderer
        self._markup = markup
        self._collapsed = False
        self._count = 0
        self._header = PanelHeader(id=f"{name}-header", classes="live-panel-header")
        self._body = Static(id=f"{name}-body", classes="live-panel-body")

    def compose(self):
        yield self._header
        yield self._body

    def on_panel_header_clicked(self, event: PanelHeader.Clicked) -> None:
        """Toggle collapse when the header is clicked."""
        self._collapsed = not self._collapsed
        self._body.display = not self._collapsed
        # The header is docked, so it never counts toward the panel's
        # auto-height. With the body hidden there are no non-docked children
        # left, so auto-height resolves to zero and the whole panel — title and
        # its click target included — vanishes. While collapsed, pin an
        # explicit height big enough for the title: one row for the title plus
        # one for the `border-top: tall` separator (box-sizing is border-box).
        self.styles.height = 2 if self._collapsed else "auto"
        self._update_header()

    def _update_header(self) -> None:
        glyph = "▸" if self._collapsed else "▾"
        self._header.update(
            Content.from_markup(
                f"[b $accent]{glyph} {self._title}[/] [dim]({self._count})[/]"
            )
        )

    def _render_items(self, items: list) -> None:
        """Render the current items, or hide the panel when there are none."""
        if not items:
            self.display = False
            self._header.update("")
            self._body.update("")
            return
        self.display = True
        self._count = len(items)
        self._update_header()
        text = self._renderer(items)
        self._body.update(Content.from_markup(text) if self._markup else Content(text))


class TaskPanel(LivePanel):
    """The agent's live checklist."""

    def __init__(self) -> None:
        from ....tasks import render_tasks

        super().__init__(name="task", title="Tasks", renderer=render_tasks)

    def show_tasks(self, items: list) -> None:
        self._render_items(items)


class JobPanel(LivePanel):
    """The session's live background jobs."""

    def __init__(self) -> None:
        from ....jobs import render_jobs

        super().__init__(name="job", title="Jobs", renderer=render_jobs)

    def show_jobs(self, jobs: list) -> None:
        self._render_items(jobs)


class QueuePanel(LivePanel):
    """Messages queued to run after the current turn."""

    def __init__(self) -> None:
        from ..queue import render_queue

        super().__init__(name="queue", title="Queued", renderer=render_queue,
                         markup=True)

    def show_queue(self, items: list, paused: bool = False) -> None:
        self._title = "Queued — paused" if paused else "Queued"
        self._render_items(items)
