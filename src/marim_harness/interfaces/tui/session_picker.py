"""A modal for browsing and switching saved sessions: a filter box over the
workspace's session list, newest-first, with the active session pre-highlighted.

Session data is fetched synchronously before the modal is constructed (listing
only parses each file's JSON header, never the full messages array — see
``session/store.py``'s ``_header_fields``), so unlike ``ModelPickerModal`` there
is no async loading state to manage here."""

import time

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.message import Message
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...interfaces.durations import format_duration
from ...session import SessionInfo, filter_sessions

_NAME_WIDTH = 28
# Mirrors HarnessApp._QUIT_CONFIRM_WINDOW: a second same-row `d` within this
# window confirms; anything after it is treated as a fresh first press.
_DELETE_CONFIRM_WINDOW = 2.0


def _format_row(info: SessionInfo, active: str | None) -> str:
    name = info.name if len(info.name) <= _NAME_WIDTH else info.name[: _NAME_WIDTH - 1] + "…"
    when = info.updated[:16].replace("T", " ") if info.updated else "—"
    duration = (
        format_duration(info.duration_seconds) if info.duration_seconds is not None else "—"
    )
    marker = "  ← active" if info.id == active else ""
    return (
        f"{name:<{_NAME_WIDTH}}  {info.message_count:>3} msgs · "
        f"{info.tokens:>6} tok · {duration:>6} · {when}{marker}"
    )


class SessionPickerModal(ModalScreen[str | None]):
    """Dismisses with the chosen session id, or None if cancelled."""

    class Deleted(Message):
        """Posted once a delete is confirmed (second `d` within the window).
        The caller (HarnessApp) owns the actual SessionManager.delete() call —
        this modal only knows the SessionInfo list it was given, not a manager."""

        def __init__(self, session_id: str) -> None:
            self.session_id = session_id
            super().__init__()

    CSS = """
    SessionPickerModal {
        align: center middle;
    }
    #session-box {
        width: 90%;
        max-width: 120;
        height: auto;
        max-height: 80%;
        padding: 1 2;
        border: round $accent;
        background: $surface;
    }
    #session-title {
        text-style: bold;
        color: $accent;
        margin-bottom: 1;
    }
    #session-status {
        color: $text-muted;
    }
    #session-options {
        height: auto;
        max-height: 20;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel"), ("d", "delete", "Delete")]

    def __init__(self, sessions: list[SessionInfo], active: str | None = None) -> None:
        super().__init__()
        self.sessions = sessions
        self.active = active
        self._armed: tuple[str, float] | None = None  # (session_id, armed_at)

    def compose(self) -> ComposeResult:
        with Vertical(id="session-box"):
            yield Static("Switch session", id="session-title")
            yield Input(placeholder="filter… (Tab to navigate, Enter to pick)",
                        id="session-filter")
            yield Static("", id="session-status")
            yield OptionList(id="session-options")

    def on_mount(self) -> None:
        self._populate(self.sessions)
        self.query_one("#session-filter", Input).focus()

    def _populate(self, sessions: list[SessionInfo]) -> None:
        options = self.query_one("#session-options", OptionList)
        options.clear_options()
        active_index = None
        for i, info in enumerate(sessions):
            options.add_option(Option(_format_row(info, self.active), id=info.id))
            if info.id == self.active:
                active_index = i
        if sessions:
            options.highlighted = active_index if active_index is not None else 0

    def _highlighted_id(self) -> str | None:
        options = self.query_one("#session-options", OptionList)
        if options.option_count and options.highlighted is not None:
            return options.get_option_at_index(options.highlighted).id
        return None

    def _set_status(self, text: str) -> None:
        self.query_one("#session-status", Static).update(text)

    def on_input_changed(self, event: Input.Changed) -> None:
        self._armed = None
        self._populate(filter_sessions(self.sessions, event.value))

    def on_input_submitted(self, event: Input.Submitted) -> None:
        choice = self._highlighted_id()
        if choice is not None:
            self.dismiss(choice)

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(event.option.id)

    def on_option_list_option_highlighted(self, event: OptionList.OptionHighlighted) -> None:
        # Any highlight change (arrow/home/end/pageup/pagedown) cancels an
        # in-progress arm — the second `d` must land on the SAME row.
        if self._armed is not None and self._armed[0] != event.option_id:
            self._armed = None
            self._set_status("")

    def action_delete(self) -> None:
        session_id = self._highlighted_id()
        if session_id is None:
            return
        if session_id == self.active:
            self._armed = None
            self._set_status("Can't delete the active session.")
            return
        now = time.monotonic()
        if self._armed is not None and self._armed[0] == session_id and \
                now - self._armed[1] <= _DELETE_CONFIRM_WINDOW:
            self._armed = None
            self.sessions = [s for s in self.sessions if s.id != session_id]
            self._populate(self.sessions)
            self._set_status(f"Deleted {session_id}.")
            self.post_message(self.Deleted(session_id))
            return
        self._armed = (session_id, now)
        self._set_status("Press d again to delete.")

    def action_cancel(self) -> None:
        self.dismiss(None)
