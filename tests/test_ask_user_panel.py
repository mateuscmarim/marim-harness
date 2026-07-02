# tests/test_ask_user_panel.py
import pytest
from textual.app import App, ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Button, SelectionList, Static

from marim_harness.ask_user import Choice, Question
from marim_harness.interfaces.tui.ask_user import AskUserPanel
from marim_harness.interfaces.tui.interaction_panel import run_panel


class _Harness(App):
    """Mimics the main screen's stack: scrollable #log, then #status-bar —
    run_panel mounts the panel between them."""

    def __init__(self, questions):
        super().__init__()
        self._questions = questions
        self.result = "unset"

    def compose(self) -> ComposeResult:
        yield VerticalScroll(Static("line\n" * 100), id="log")
        yield Static("", id="status-bar")

    def on_mount(self) -> None:
        self.run_worker(self._ask())

    async def _ask(self) -> None:
        self.result = await run_panel(self, AskUserPanel(self._questions))


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
    # Taller viewport: the panel's inherited `max-height: 50%` clamps it below
    # the multi-select layout's natural height (SelectionList + Confirm button),
    # which would otherwise render the button below row 24 and make pilot.click
    # raise OutOfBounds.
    async with app.run_test(size=(80, 36)) as pilot:
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
        app.query_one("#ask-other").focus()
        await pilot.pause()
        app.query_one("#ask-other").value = "custom thing"
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
    # See test_multi_select_confirm_returns_list: taller viewport so the
    # Confirm button clears the panel's max-height clamp and stays clickable.
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()
        # Click Confirm without selecting anything or typing free-text
        await pilot.click("#ask-confirm")
        await pilot.pause()
        # Panel must NOT have dismissed: result stays at sentinel
        assert app.result == "unset"
        # The SelectionList must still be mounted
        assert app.query_one("#ask-select", SelectionList) is not None


@pytest.mark.anyio
async def test_multi_select_empty_confirm_then_valid_confirm():
    """After an empty-confirm no-op, the user can still complete the question."""
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    # See test_multi_select_confirm_returns_list: taller viewport so the
    # Confirm button clears the panel's max-height clamp and stays clickable.
    async with app.run_test(size=(80, 36)) as pilot:
        await pilot.pause()
        # First click: empty — ignored
        await pilot.click("#ask-confirm")
        await pilot.pause()
        assert app.result == "unset"
        # Directly toggle option 0 via the SelectionList API then confirm
        sel = app.query_one("#ask-select", SelectionList)
        sel.select(0)
        await pilot.pause()
        app.query_one("#ask-confirm", Button).press()
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


@pytest.mark.anyio
async def test_transcript_scrolls_while_question_pending():
    """The whole point of the panel: with focus on the panel's OptionList,
    PageDown scrolls the transcript (priority binding beats the OptionList's
    own paging) and the question stays pending."""
    qs = [Question("Pick one", "Pick", [Choice("Alpha"), Choice("Beta")])]
    app = _Harness(qs)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        log = app.query_one("#log", VerticalScroll)
        assert log.scroll_y == 0
        await pilot.press("pagedown")
        await pilot.pause()
        assert log.scroll_y > 0
        assert app.result == "unset"


@pytest.mark.anyio
async def test_late_event_after_last_answer_does_not_raise():
    """A second event (OptionSelected/Input.Submitted/Button.Pressed) racing
    the panel's removal after the final _record already resolved ``result``
    must not IndexError — _index is past the end of _questions by then."""
    from textual.widgets import Button, Input, OptionList
    from textual.widgets.option_list import Option

    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        await pilot.press("enter")  # answers the only question -> resolves
        await pilot.pause()
        assert panel.result.done()

        # Stand-in event sources: they don't need to be mounted anywhere —
        # the handlers are called directly to simulate an event delivered
        # after resolution but before the panel is torn down.
        panel.on_input_submitted(Input.Submitted(Input(), "late"))
        panel.on_option_list_option_selected(
            OptionList.OptionSelected(OptionList(), Option("x", id="0"), 0)
        )
        panel.on_button_pressed(Button.Pressed(Button(id="ask-confirm")))
    assert app.result == {"Pick": "Alpha"}


@pytest.mark.anyio
async def test_panel_removed_after_answer():
    qs = [Question("Pick one", "Pick", [Choice("Alpha")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
        assert not app.query(AskUserPanel)


@pytest.mark.anyio
async def test_option_list_shows_three_options_and_scrolls():
    """Long option lists cap at three visible options (each 2 rows here: label
    + description) and scroll internally — the scrollbar belongs to the list,
    and the question and free-text input stay pinned inside the panel."""
    qs = [Question("Pick one", "Pick",
                   [Choice(f"opt {i}", "a description") for i in range(8)])]
    app = _Harness(qs)
    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.pause()
        panel = app.query_one(AskUserPanel)
        opts = app.query_one("#ask-options")
        # 3 options x 2 rows + the OptionList's own tall border
        assert opts.region.height == 8
        assert opts.show_vertical_scrollbar
        # the panel itself does not scroll; the input stays inside it
        assert not panel.show_vertical_scrollbar
        other = app.query_one("#ask-other")
        assert other.region.bottom < panel.region.bottom


@pytest.mark.anyio
async def test_short_option_list_is_not_padded():
    """A two-option list takes only its own height — the 3-option cap is a
    maximum, not a fixed size."""
    qs = [Question("Pick one", "Pick", [Choice("a"), Choice("b")])]
    app = _Harness(qs)
    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.pause()
        opts = app.query_one("#ask-options")
        # 2 single-row options + border
        assert opts.region.height == 4
        assert not opts.show_vertical_scrollbar


@pytest.mark.anyio
async def test_selection_list_caps_at_three_visible_options():
    """The 3-option cap applies to multi-select too."""
    qs = [Question("Pick many", "Feat",
                   [Choice(f"o{i}") for i in range(6)], multi=True)]
    app = _Harness(qs)
    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.pause()
        sel = app.query_one("#ask-select", SelectionList)
        # 3 single-row options + the list's tall border
        assert sel.region.height == 5
        assert sel.show_vertical_scrollbar


@pytest.mark.anyio
async def test_confirm_button_shows_selected_count():
    qs = [Question("Pick many", "Feat", [Choice("a"), Choice("b")], multi=True)]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        confirm = app.query_one("#ask-confirm", Button)
        assert str(confirm.label) == "Confirm selection"
        await pilot.press("space")  # check the highlighted option
        await pilot.pause()
        assert str(confirm.label) == "Confirm selection (1)"
        await pilot.press("space")  # uncheck it again
        await pilot.pause()
        assert str(confirm.label) == "Confirm selection"


@pytest.mark.anyio
async def test_more_options_hint_when_list_overflows():
    qs = [Question("Pick one", "Pick", [Choice(f"o{i}") for i in range(8)])]
    app = _Harness(qs)
    async with app.run_test(size=(80, 50)) as pilot:
        await pilot.pause()
        more = app.query_one("#ask-more", Static)
        assert more.display
        assert "+5 more options" in str(more.render())


@pytest.mark.anyio
async def test_no_more_options_hint_when_list_fits():
    qs = [Question("Pick one", "Pick", [Choice("a"), Choice("b")])]
    app = _Harness(qs)
    async with app.run_test() as pilot:
        await pilot.pause()
        assert not app.query_one("#ask-more", Static).display
