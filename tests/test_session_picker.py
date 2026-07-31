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
