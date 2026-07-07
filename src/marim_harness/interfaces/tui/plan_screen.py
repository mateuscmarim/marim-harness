"""The full-height plan overlay (Ctrl+P): a read-only view of the current plan's
summary, its steps with live progress markers, and the plan-file path. Pushed as
a Screen (like Settings and the sub-agents view), NOT a ModalScreen — you summon
it precisely to *not* need the transcript behind it, so the inline-over-modal
rule doesn't apply. Step progress is read from the live task list, so the view
reflects execution in real time; the summary/path come from deps.plan."""

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import VerticalScroll
from textual.content import Content
from textual.markup import escape
from textual.screen import Screen
from textual.widgets import Static

from ...tasks import Task, render_tasks


class PlanScreen(Screen[None]):
    """Read-only plan overlay. Esc closes it."""

    CSS = """
    PlanScreen { background: $surface; }
    #plan-screen-header { height: 1; padding: 0 1; background: $panel; color: $accent;
        text-style: bold; }
    #plan-screen-body { height: 1fr; padding: 1 2; }
    #plan-screen-summary { margin-bottom: 1; }
    #plan-screen-steps { margin-bottom: 1; }
    #plan-screen-path { color: $text-muted; }
    #plan-screen-footer { height: 1; padding: 0 1; background: $panel; color: $text-muted; }
    """

    BINDINGS = [Binding("escape", "close", "Close", show=False)]

    def __init__(self, summary: str, path: str | None, tasks: list[Task]) -> None:
        super().__init__()
        self._summary = summary
        self._path = path
        self._tasks = tasks

    def compose(self) -> ComposeResult:
        yield Static("Plan", id="plan-screen-header")
        with VerticalScroll(id="plan-screen-body"):
            # The summary is model-supplied free text — it must NOT be parsed as
            # Textual markup. An unterminated "[" (e.g. "handle [edit without
            # close") raises MarkupError if this goes through markup parsing, and
            # escape() does not help: it only escapes bracket sequences that have
            # a closing "]", so an unterminated bracket still crashes. markup=False
            # renders the text literally and safely.
            yield Static(self._summary, id="plan-screen-summary", markup=False)
            yield Static(Content(render_tasks(self._tasks)), id="plan-screen-steps")
            # Unlike the summary, the path is a slugified-summary-derived filename
            # (brackets stripped upstream), so escape() alone is sufficient here —
            # it is never free-form model text that could contain an unterminated
            # bracket or a bracket + key='value' sequence.
            path_line = (
                f"[dim]file:[/] {escape(self._path)}" if self._path else "[dim]not saved to disk[/]"
            )
            yield Static(Content.from_markup(path_line), id="plan-screen-path")
        yield Static("esc close", id="plan-screen-footer")

    def action_close(self) -> None:
        self.dismiss()
