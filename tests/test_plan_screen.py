"""PlanScreen overlay: shows the plan summary, path, and the checklist with live
progress markers; Ctrl+P is a no-op hint when no plan exists."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.plan_screen import PlanScreen
from marim_harness.tasks import Task

pytestmark = pytest.mark.anyio


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield Static("base")


async def test_plan_screen_shows_summary_path_and_progress():
    tasks = [Task(text="Extract tokenizer", status="done"),
             Task(text="Add tests", status="in_progress")]
    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(PlanScreen("Refactor the parser", "/tmp/plan.md", tasks))
        await pilot.pause()
        text = " ".join(str(w.content) for w in app.screen.query(Static))
        assert "Refactor the parser" in text
        assert "/tmp/plan.md" in text
        assert "Extract tokenizer" in text
        assert "✔" in text   # done marker
        assert "▸" in text   # in-progress marker


async def test_escape_dismisses():
    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(PlanScreen("s", None, [Task(text="x")]))
        await pilot.pause()
        assert isinstance(app.screen, PlanScreen)
        await pilot.press("escape")
        await pilot.pause()
        assert not isinstance(app.screen, PlanScreen)


async def test_summary_with_unterminated_bracket_does_not_raise_markup_error():
    """The summary is model-supplied free text and must never be parsed as
    Textual markup. This exact bracket + bareword + quoted ``key='value'``
    pattern, left unterminated, reliably raises MarkupError under Textual's
    default markup parsing (confirmed empirically against Textual 8.2.7,
    including with escape() applied) — so this is a genuine regression check:
    if ``markup=False`` were ever dropped from the summary's Static, this test
    would fail with MarkupError instead of silently passing."""
    summary = "Handle [edit id=1 old_string='foo' with no close"
    app = _Harness()
    async with app.run_test() as pilot:
        app.push_screen(PlanScreen(summary, None, [Task(text="x")]))
        await pilot.pause()
        text = " ".join(str(w.content) for w in app.screen.query(Static))
        assert "Handle [edit id=1 old_string='foo' with no close" in text
