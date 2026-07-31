import pytest
from textual.app import App
from textual.widgets import OptionList

from marim_harness.interfaces.tui.session_picker import SessionPickerModal
from marim_harness.session import SessionInfo

_SESSIONS = [
    SessionInfo(id="s-alpha", name="Fix auth bug", updated="2026-07-03T10:00:00",
                message_count=5, tokens=1200, duration_seconds=125.0),
    SessionInfo(id="s-beta", name="Refactor session store", updated="2026-07-02T09:00:00",
                message_count=12, tokens=8300, duration_seconds=None),
    SessionInfo(id="s-gamma", name="20260701-120000", updated="2026-07-01T12:00:00",
                message_count=1, tokens=0, duration_seconds=5.0),
]


class _Host(App):
    def __init__(self, sessions, active=None):
        super().__init__()
        self.sessions = sessions
        self.active = active
        self.result = "unset"

    def on_mount(self) -> None:
        self.run_worker(self._pick())

    async def _pick(self) -> None:
        self.result = await self.push_screen_wait(
            SessionPickerModal(self.sessions, active=self.active)
        )


@pytest.mark.anyio
async def test_opens_with_all_sessions_listed():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)


@pytest.mark.anyio
async def test_active_session_is_highlighted_on_open():
    app = _Host(_SESSIONS, active="s-beta")
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.highlighted is not None
        assert opts.get_option_at_index(opts.highlighted).id == "s-beta"


@pytest.mark.anyio
async def test_typing_filters_by_name():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "auth":
            await pilot.press(ch)
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == 1
        assert opts.get_option_at_index(0).id == "s-alpha"


@pytest.mark.anyio
async def test_enter_in_filter_picks_highlighted():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        for ch in "refactor":
            await pilot.press(ch)
        await pilot.pause()
        await pilot.press("enter")
        await pilot.pause()
    assert app.result == "s-beta"


@pytest.mark.anyio
async def test_escape_cancels_with_none():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("escape")
        await pilot.pause()
    assert app.result is None


@pytest.mark.anyio
async def test_option_selected_dismisses_with_id():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        await pilot.press("tab")  # move focus from Input to OptionList
        await pilot.press("down")  # highlight s-beta
        await pilot.press("enter")  # OptionList's own enter -> select
        await pilot.pause()
    assert app.result == "s-beta"


def test_row_shows_msgs_tokens_duration_and_updated():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[0], active=None)
    assert "5" in row and "msgs" in row
    assert "1200" in row
    assert "2m" in row  # format_duration(125.0) == "2m"
    assert "2026-07-03 10:00" in row


def test_row_marks_active_session():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[0], active="s-alpha")
    assert "active" in row.lower()
    other = _format_row(_SESSIONS[1], active="s-alpha")
    assert "active" not in other.lower()


def test_row_shows_dash_for_missing_duration():
    from marim_harness.interfaces.tui.session_picker import _format_row

    row = _format_row(_SESSIONS[1], active=None)  # duration_seconds=None
    assert "—" in row


@pytest.mark.anyio
async def test_first_d_arms_without_deleting():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)  # nothing removed yet
        assert "press d again" in str(modal.query_one("#session-status").render()).lower()


@pytest.mark.anyio
async def test_second_d_confirms_removes_row_and_posts_deleted():
    received: list[str] = []

    class _DeleteHost(App):
        def __init__(self, sessions):
            super().__init__()
            self.sessions = sessions

        def on_mount(self) -> None:
            self.push_screen(SessionPickerModal(self.sessions))

        def on_session_picker_modal_deleted(self, message: SessionPickerModal.Deleted) -> None:
            received.append(message.session_id)

    app = _DeleteHost(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS) - 1
    assert received == ["s-alpha"]


@pytest.mark.anyio
async def test_active_session_cannot_be_armed():
    app = _Host(_SESSIONS, active="s-alpha")
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")  # highlighted starts on s-alpha (the active one)
        await pilot.press("d")
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)
        assert "can't delete the active session" in str(
            modal.query_one("#session-status").render()
        ).lower()


@pytest.mark.anyio
async def test_option_rows_do_not_wrap():
    # Regression test: at narrow terminal widths (confirmed at 80/100 cols via
    # tmux smoke test), _format_row's fixed-width string (up to 89 cols) wraps
    # onto a second line inside the OptionList instead of being clipped, which
    # looks broken. Assert the OptionList's own resolved style carries the
    # no-wrap/ellipsis fix. This must be read off `opts.styles` (the widget's
    # own resolved style), not `get_component_styles("option-list--option")`:
    # OptionList's line-height/wrapping computation
    # (`_update_lines`/`Visual.to_strips`) reads the widget's own `styles`
    # directly, so a component-class-scoped rule resolves fine but has no
    # effect on actual wrapping — verified by direct experimentation against
    # the installed Textual source.
    app = _Host(_SESSIONS)
    async with app.run_test(size=(80, 24)) as pilot:
        await pilot.pause()
        modal = app.screen
        opts = modal.query_one("#session-options", OptionList)
        assert opts.styles.text_wrap == "nowrap"
        assert opts.styles.text_overflow == "ellipsis"

        # Stronger, still-deterministic check: with wrapping disabled, each
        # option must render as exactly one line, so the OptionList's total
        # rendered height equals its option count. Pre-fix, at 80 cols each
        # 79+ char row wraps onto 2 lines, so this would be 2x option_count.
        assert opts.virtual_size.height == opts.option_count


@pytest.mark.anyio
async def test_moving_highlight_clears_armed_state():
    app = _Host(_SESSIONS)
    async with app.run_test() as pilot:
        await pilot.pause()
        modal = app.screen
        await pilot.press("tab")
        await pilot.press("d")  # arm s-alpha
        await pilot.press("down")  # move off it
        await pilot.press("d")  # would confirm s-alpha if still armed; must NOT
        await pilot.pause()
        opts = modal.query_one("#session-options", OptionList)
        assert opts.option_count == len(_SESSIONS)  # nothing deleted
