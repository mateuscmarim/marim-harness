"""TaskPanel shows a compact plan title above the checklist when a plan exists."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.panels import TaskPanel
from marim_harness.tasks import Task

pytestmark = pytest.mark.anyio

# An unterminated bracket sequence that escape() cannot neutralize.
# Unlike a balanced [/], ``rich``/``textual`` escape() only escapes complete tags,
# so this will still raise MarkupError if parsed as markup.
# Plan titles must use literal Content, not markup, to safely handle such text.
MARKUP_BOMB = "[/] and [edit(old_string=\"unterminated"


class _Harness(App):
    def compose(self) -> ComposeResult:
        yield TaskPanel()


async def test_plan_title_renders_above_checklist():
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        panel.show_tasks([Task(text="Extract tokenizer")], plan_title="Refactor the parser")
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).render())
        assert "Plan: Refactor the parser" in body
        assert "^P for full plan" in body
        assert "Extract tokenizer" in body


async def test_no_title_when_plan_absent():
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        panel.show_tasks([Task(text="Extract tokenizer")])  # no plan_title
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).render())
        assert "Plan:" not in body
        assert "Extract tokenizer" in body


async def test_plan_title_with_markup_like_text_does_not_crash():
    """Plan titles containing markup-like syntax (unterminated brackets) must
    render as literal text, not be parsed as markup. This ensures that
    model-supplied summaries with [brackets] cannot trigger MarkupError."""
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        # Pass a markup-bomb summary that would crash if parsed as markup
        panel.show_tasks([Task(text="normal step")], plan_title=MARKUP_BOMB)
        # If the title is parsed as markup, await pilot.pause() will raise MarkupError.
        # Reaching here proves the title rendered safely as literal content.
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).render())
        # Verify the bomb string rendered literally: check a distinctive fragment.
        assert "edit(old_string=" in body
        # Verify the checklist item still appears (title didn't clobber the steps).
        assert "normal step" in body


async def test_plan_title_truncates_long_summary():
    """Plan titles longer than 48 characters are truncated with an ellipsis."""
    app = _Harness()
    async with app.run_test() as pilot:
        panel = app.query_one(TaskPanel)
        # Use a summary longer than 48 chars (82 chars total).
        # When truncated to 47 + "…", the tail "exceeds" should not appear.
        long_summary = (
            "This is a very long plan summary that definitely "
            "exceeds forty-eight characters"
        )
        panel.show_tasks([Task(text="do work")], plan_title=long_summary)
        await pilot.pause()
        body = str(app.query_one("#task-body", Static).render())
        # Verify ellipsis is present (truncation occurred).
        assert "…" in body
        # Verify the truncated tail does not appear.
        assert "exceeds" not in body
        # Verify the beginning is still there.
        assert "This is a very long" in body
        # Verify the checklist item still appears.
        assert "do work" in body
