"""Inline interaction panels: widgets mounted above the status bar that put a
question to the user while the turn worker awaits the answer. Unlike the
ModalScreens they replaced, the transcript stays visible and scrollable —
mouse wheel reaches it natively (no modal layer eats events), and the panel
forwards PageUp/PageDown and Ctrl+Up/Down to it for keyboard users.

The awaiting side goes through :func:`run_panel`: mount, await the panel's
``result`` future, and always remove the panel in a ``finally`` — so a turn
cancelled while a panel is up (Esc/Ctrl-C) tears it down too."""

import asyncio
from typing import Any

from textual.app import App
from textual.binding import Binding
from textual.containers import Vertical, VerticalScroll


class InteractionPanel(Vertical):
    """Base for the inline ask-user/approval panels.

    Owns the ``result`` future the turn worker awaits; subclasses call
    :meth:`resolve` everywhere the old modals called ``dismiss``."""

    # priority=True: the focused child (OptionList/SelectionList) has its own
    # PageUp/PageDown bindings for paging options, which would otherwise shadow
    # these. While a panel is up, paging is for reading the transcript the
    # question refers to — that's the whole point of being inline.
    BINDINGS = [
        Binding("pageup", "scroll_transcript('page_up')", "Scroll transcript",
                priority=True, show=False),
        Binding("pagedown", "scroll_transcript('page_down')", "Scroll transcript",
                priority=True, show=False),
        Binding("ctrl+up", "scroll_transcript('up')", "Scroll transcript",
                priority=True, show=False),
        Binding("ctrl+down", "scroll_transcript('down')", "Scroll transcript",
                priority=True, show=False),
    ]

    DEFAULT_CSS = """
    InteractionPanel {
        height: auto;
        max-height: 50%;
        padding: 1 2;
        background: $surface;
        border: round $accent;
    }
    """

    def __init__(self, *, id: str | None = None) -> None:
        super().__init__(id=id)
        # Panels are always constructed inside the app's event loop (the turn
        # worker or a test coroutine), so get_running_loop is safe and avoids
        # get_event_loop's 3.12 deprecation path.
        self.result: asyncio.Future[Any] = asyncio.get_running_loop().create_future()

    def resolve(self, value: Any) -> None:
        """Resolve the awaited future once; later calls are no-ops (a double
        Enter/click must not raise InvalidStateError)."""
        if not self.result.done():
            self.result.set_result(value)

    def action_scroll_transcript(self, direction: str) -> None:
        """Forward a scroll key to the transcript. ``direction`` is the suffix
        of the Widget scroll method: page_up / page_down / up / down.
        animate=False so the position is deterministic for tests and snappy
        for readers."""
        log = self.app.query_one("#log", VerticalScroll)
        getattr(log, f"scroll_{direction}")(animate=False)


async def run_panel(app: App, panel: InteractionPanel) -> Any:
    """Mount ``panel`` above the status bar, await its result, remove it.

    Removal lives in a ``finally`` and is deliberately not awaited: when the
    turn worker is cancelled the CancelledError propagates out of the result
    await, and awaiting the removal here could be interrupted by that same
    cancellation — scheduling it is enough. Focus is restored to whatever had
    it before the panel appeared (the modals got this for free from screen
    push/pop)."""
    previous = app.focused
    await app.mount(panel, before="#status-bar")
    try:
        return await panel.result
    finally:
        panel.remove()
        if previous is not None and previous.is_attached:
            previous.focus()
