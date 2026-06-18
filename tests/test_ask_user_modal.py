# tests/test_ask_user_modal.py
import pytest
from textual.app import App
from textual.widgets import Button, SelectionList

from marim_harness.ask_user import Choice, Question
from marim_harness.interfaces.tui.ask_user import AskUserModal


class _Harness(App):
    def __init__(self, questions):
        super().__init__()
        self._questions = questions
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await self.push_screen_wait(AskUserModal(self._questions))


@pytest.mark.anyio
async def test_single_select_returns_highlighted_label():
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # highlighted is index 0
        await pilot.pause()
    assert app.result == {"Pick": "Alpha"}


@pytest.mark.anyio
async def test_single_select_second_option():
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == {"Pick": "Beta"}


@pytest.mark.anyio
async def test_multi_select_confirm_returns_list():
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("space")  # toggle highlighted (index 0)
        await pilot.click("#ask-confirm")
        await pilot.pause()
    assert app.result == {"Feat": ["a"]}


@pytest.mark.anyio
async def test_free_text_answer():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        # In Textual 8.x, app.query_one searches the base screen, not the modal;
        # use app.screen.query_one to reach widgets inside the active ModalScreen.
        app.screen.query_one("#ask-other").focus()
        await pilot.pause()
        app.screen.query_one("#ask-other").value = "custom thing"
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == {"Pick": "custom thing"}


@pytest.mark.anyio
async def test_escape_cancels():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_multi_select_empty_confirm_is_ignored():
    """Confirm with nothing checked and no free-text must not dismiss or advance."""
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        # Click Confirm without selecting anything or typing free-text
        await pilot.click("#ask-confirm")
        await pilot.pause()
        # Modal must NOT have dismissed: result stays at sentinel
        assert app.result == "unset"
        # The SelectionList must still be mounted
        assert app.screen.query_one("#ask-select", SelectionList) is not None


@pytest.mark.anyio
async def test_multi_select_empty_confirm_then_valid_confirm():
    """After an empty-confirm no-op, the user can still complete the question."""
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        # First click: empty — ignored
        await pilot.click("#ask-confirm")
        await pilot.pause()
        assert app.result == "unset"
        # Directly toggle option 0 via the SelectionList API then confirm
        sel = app.screen.query_one("#ask-select", SelectionList)
        sel.select(0)
        await pilot.pause()
        app.screen.query_one("#ask-confirm", Button).press()
        await pilot.pause()
    assert app.result == {"Feat": ["a"]}


@pytest.mark.anyio
async def test_multi_question_steps_and_collects():
    qs = [
        Question("First?", "One", [Choice("a1"), Choice("a2")]),
        Question("Second?", "Two", [Choice("b1"), Choice("b2")]),
    ]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")  # Q1 -> a1
        await pilot.pause()
        await pilot.press("down")
        await pilot.press("enter")  # Q2 -> b2
        await pilot.pause()
    assert app.result == {"One": "a1", "Two": "b2"}
