"""TaskPanel shows a compact plan title above the checklist when a plan exists."""

import pytest
from textual.app import App, ComposeResult
from textual.widgets import Static

from marim_harness.interfaces.tui.widgets.panels import TaskPanel
from marim_harness.tasks import Task

pytestmark = pytest.mark.anyio


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
