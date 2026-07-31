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
        /* horizontal-only padding: the round border already separates the
           panel vertically, and rows are scarce while a panel is up */
        padding: 0 2;
        background: $surface;
        border: round $accent;
        /* content taller than the clamp scrolls instead of clipping */
        overflow-y: auto;
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


def _focus_target(panel: InteractionPanel) -> Any:
    """The widget that makes ``panel`` keyboard-reachable: the panel itself if
    it's focusable (ApprovalPanel/TrustPanel set ``can_focus = True``), else
    its own first focusable descendant in DOM order. AskUserPanel/PlanCard
    never set ``can_focus`` on themselves — they focus a SelectionList/
    OptionList directly in ``on_mount`` — so ``panel.focus()`` on one of those
    would silently no-op: Textual's ``Screen.set_focus`` only moves focus when
    ``widget.focusable`` is True. None if the panel has no focusable target
    yet (e.g. an AskUserPanel whose option-mounting worker hasn't run yet)."""
    if panel.focusable:
        return panel
    return next((w for w in panel.query("*") if w.focusable), None)


async def run_panel(app: App, panel: InteractionPanel) -> Any:
    """Mount ``panel`` above the status bar, await its result, remove it.

    Removal lives in a ``finally`` and is deliberately not awaited: when the
    turn worker is cancelled the CancelledError propagates out of the result
    await, and awaiting the removal here could be interrupted by that same
    cancellation — scheduling it is enough. Focus goes to a still-pending
    sibling panel if one exists, else to whatever had focus before this panel
    appeared (the modals got the latter for free from screen push/pop).

    Two things must happen before the mount, both because #status-bar and the
    sub-agents viewer live in the *base* screen (index 0 of the stack), not
    necessarily the top one:

    - The base screen is targeted explicitly rather than via ``app.mount``,
      which delegates to ``app.screen`` (the top of the stack). The settings
      screen and model picker are ModalScreens the user can open mid-turn; if
      an approval/ask_user fires while one is on top, ``app.mount`` would look
      for #status-bar on the modal and raise NoMatches, killing the turn.
    - The ctrl+x sub-agents viewer, when open, covers the base screen on its
      own layer — a panel mounted underneath it would be invisible yet still
      grab focus, and the viewer's Esc ("back") would land on it instead of
      cancelling/denying as intended. So an open viewer is closed first. This
      is the one generic thing run_panel knows about the real app; test
      harnesses have no ``subagents`` collaborator, hence the guard.
    """
    subagents = getattr(app, "subagents", None)
    if subagents is not None and getattr(subagents, "open", False):
        subagents.close()
    previous = app.focused
    base = app.screen_stack[0]
    bar = base.query_one("#status-bar")
    await base.mount(panel, before=bar)
    try:
        return await panel.result
    finally:
        panel.remove()
        # A second panel can be pending: pydantic-ai runs tool calls
        # concurrently (sequential defaults to False), and the trust prompt is
        # not gated on turn_busy. Hand focus to it rather than to `previous` —
        # app.on_descendant_focus declines to redirect focus while any
        # InteractionPanel is mounted, so a panel that loses focus never gets it
        # back: its a/d keys would type into the prompt and Esc would cancel the
        # whole turn instead of answering it.
        #
        # `panel.remove()` above is scheduled, not awaited (see this function's
        # docstring), so `panel` may still be in the DOM here — hence the
        # identity guard. app.query returns DOM order, and each panel is mounted
        # `before=bar`, so the first match is the OLDEST pending panel: with
        # three panels up the focus order is deterministic and testable.
        #
        # _focus_target, not sibling.focus() directly: AskUserPanel/PlanCard
        # aren't focusable themselves (they focus an OptionList/SelectionList
        # descendant instead), so focusing the sibling widget would silently
        # no-op for those two panel types and leave them just as unreachable.
        sibling = next(
            (p for p in app.query(InteractionPanel) if p is not panel), None
        )
        target = _focus_target(sibling) if sibling is not None else None
        if target is not None:
            target.focus()
        elif previous is not None and previous.is_attached:
            previous.focus()
