# tests/test_plan_card.py
"""PlanCard inline panel: renders summary + steps + choices, resolves to the
chosen label, defaults to 'Keep planning' on Escape."""

import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from marim_harness.ask_user import Choice
from marim_harness.interfaces.tui.interaction_panel import run_panel
from marim_harness.interfaces.tui.plan_card import PlanCard

pytestmark = pytest.mark.anyio

_CHOICES = [
    Choice("Execute hands-off (auto)", "Run the whole plan."),
    Choice("Execute step-by-step (ask)", "Approve each change."),
    Choice("Hand off to sub-agent", "Spawn a sub-agent."),
    Choice("Keep planning", "Save as a draft."),
]


class _Harness(App):
    def __init__(self, summary, steps, choices):
        super().__init__()
        self._args = (summary, steps, choices)
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._run())

    async def _run(self) -> None:
        self.result = await run_panel(self, PlanCard(*self._args))


async def test_selects_highlighted_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer", "Add tests"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # first option highlighted
        await pilot.pause()
    assert app.result == "Execute hands-off (auto)"


async def test_selects_second_choice():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "Execute step-by-step (ask)"


async def test_escape_keeps_planning():
    app = _Harness("Refactor parser", ["Extract tokenizer"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result == "Keep planning"


async def test_renders_summary_and_steps():
    app = _Harness("Refactor the parser", ["Extract tokenizer", "Add tests"], _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        text = card.query_one("#plan-summary", Static).content
        body = card.query_one("#plan-steps", Static).content
        assert "Refactor the parser" in str(text)
        assert "Extract tokenizer" in str(body)
        assert "1." in str(body)  # steps are numbered


async def test_escapes_brackets_in_summary_and_steps():
    """Regression: summary and steps with brackets (e.g. list[str], [/] paths)
    must not crash with MarkupError or corrupt rendering."""
    summary = "Refactor to handle list[str] outputs"
    steps = [
        "Fix the redirect for [/] to home",
        "Support [bold] tags in description",
    ]
    app = _Harness(summary, steps, _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        # Verify the content renders literally, not parsed as markup
        summary_text = card.query_one("#plan-summary", Static).content
        assert "list[str]" in str(summary_text)
        steps_body = card.query_one("#plan-steps", Static).content
        assert "[/]" in str(steps_body)
        assert "[bold]" in str(steps_body)
        # Verify selection still works (no crash, choice resolves)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "Execute hands-off (auto)"


async def test_unterminated_bracket_with_quote_does_not_crash():
    """Regression: escape() only neutralizes bracket sequences that have a
    closing ']'. An unterminated '[' followed by a quoted key='value' (as a
    model might emit describing a tool call, e.g. "Apply edit
    [old_string='foo") slips through escape() unescaped and crashes
    Content.from_markup with MarkupError. Steps must render as literal text."""
    summary = "Apply a risky edit"
    steps = ["Apply edit [old_string='foo", "Verify the change"]
    app = _Harness(summary, steps, _CHOICES)
    async with app.run_test() as pilot:
        await pilot.pause()
        card = app.query_one(PlanCard)
        steps_body = card.query_one("#plan-steps", Static).content
        assert "old_string='foo" in str(steps_body)
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "Execute hands-off (auto)"
