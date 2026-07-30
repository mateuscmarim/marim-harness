"""The app-side half of the pending-message queue.

``TurnQueue`` (queue.py) owns the ordering, ids and paused flag and is free of
Textual; this owns everything the *app* does around it — repainting the queue
display, draining the next item into a turn, and the post-turn hand-off that
decides between draining and waking. The App keeps only the Textual surface:
the ``action_*`` entry points bound to keys and to the panel's click links.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from textual.css.query import NoMatches

from .queue import QueuedMessage, TurnQueue
from .widgets import ErrorMessage, PromptInput
from .widgets.queue_display import QueueDisplay

if TYPE_CHECKING:
    from .app import HarnessApp

logger = logging.getLogger(__name__)


class QueueController:
    """The queue as the app sees it: the collection surface delegates to the
    inner ``TurnQueue``, and every mutation that should be visible repaints the
    display, so no caller has to remember to."""

    def __init__(self, app: HarnessApp) -> None:
        self._app = app
        self._queue = TurnQueue()

    # --- the collection surface (delegated to the pure TurnQueue) ---

    @property
    def items(self) -> list[QueuedMessage]:
        return self._queue.items

    @property
    def paused(self) -> bool:
        return self._queue.paused

    @paused.setter
    def paused(self, value: bool) -> None:
        self._queue.paused = value

    def __bool__(self) -> bool:
        return bool(self._queue)

    def enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._queue.enqueue(text, attachments)
        self.render()

    def remove(self, id: str) -> None:
        """Drop a pending queued message before it runs."""
        self._queue.remove(id)
        self.render()

    # --- the effects ---

    def render(self) -> None:
        """Repaint the queue display from the current queue."""
        if not self._app.is_running:
            return
        try:
            display = self._app.query_one(QueueDisplay)
        except NoMatches:
            return  # tearing down; nothing to paint
        display.items = list(self._queue.items)
        display.paused = self._queue.paused

    async def drain_next(self) -> None:
        """Pop and start the next queued message."""
        item = self._queue.pop_next()
        self.render()
        await self._app.start_turn(item.text, item.attachments)

    async def resume(self) -> None:
        """Resume a paused queue: clear the pause and start the next item."""
        if self._queue and not self._app.turn_busy:
            self._queue.paused = False
            await self.drain_next()

    async def edit_in_prompt(self, id: str) -> None:
        """Pop a queued message out of the queue and load it into the prompt input
        for editing — text and image attachments both, so an edit round-trips
        without losing the images (their ``[Image #N]`` markers ride along in the
        text)."""
        item = self._queue.take(id)
        if item is None:
            return
        self.render()
        prompt = self._app.query_one(PromptInput)
        prompt.text = item.text
        prompt.load_attachments(item.attachments or [])
        # Drop the paste stash along with the old draft: it belongs to whatever
        # was in the box before, and a stale entry would leave a dangling
        # [Pasted text #N] marker (or make a hand-typed #1 resurrect it).
        prompt.pastes = []
        prompt.move_cursor(prompt.document.end)
        prompt.focus()

    async def after_turn(self) -> None:
        """Called from _run_turn's finally. Drain the next queued item on a
        clean, unpaused turn; otherwise fall through to the background-job wake."""
        # A steer that landed in the finishing gap (never flushed onto a live
        # run) falls back to the front of the queue so it runs next — kept even
        # on a paused (cancel/error) finish, matching how the queue itself is
        # preserved on pause; the drain below stays gated so it waits for resume.
        leftover = self._app.harness.take_buffered_steers()
        if leftover:
            for text, atts in reversed(leftover):
                self._queue.prepend(text, atts)
            self.render()
        # after_turn runs from _run_turn's finally; an exception escaping here
        # would kill the worker before it unwinds cleanly. Draining starts the
        # next turn (worker scheduling, widget mounts) and the wake path touches
        # jobs — both can fail. Pause the queue and surface the error rather than
        # let it propagate out of the finally and strand the session.
        try:
            if not self._queue.paused and self._queue:
                await self.drain_next()
            else:
                self._app.activity.maybe_wake()
        except Exception as exc:
            self._queue.paused = True
            self._app.append_log(ErrorMessage(f"failed to start next turn: {exc}"))
            logger.warning("failed to start next turn", exc_info=True)
