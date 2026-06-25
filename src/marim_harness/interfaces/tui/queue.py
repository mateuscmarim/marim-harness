"""The TUI message queue: messages the user submitted while a turn was running,
held to run as their own turns after the current one. In-memory, process-scoped."""

from dataclasses import dataclass

from textual.markup import escape


@dataclass
class QueuedMessage:
    """One buffered user submission. ``attachments`` mirrors the tuple list
    ``Harness.run_turn`` accepts; ``id`` is a stable, per-app sequence string
    used to target the item from the panel's controls."""

    text: str
    attachments: list[tuple[bytes, str]] | None
    id: str


def render_queue(items: list[QueuedMessage]) -> str:
    """Render the pending items as a numbered Textual-markup string with
    per-item edit/remove action links. User text is escaped so brackets in a
    prompt are not parsed as markup; the ids are numeric and safe inline."""
    lines = []
    for i, m in enumerate(items, 1):
        n = len(m.attachments or [])
        tag = f" 📎{n}" if n else ""
        lines.append(
            f"{i}. {escape(m.text)}{tag}  "
            f"[@click=app.edit_queued('{m.id}')]edit[/] "
            f"[@click=app.remove_queued('{m.id}')]✕[/]"
        )
    return "\n".join(lines)


class TurnQueue:
    """The in-memory queue of user submissions buffered while a turn is running,
    held to run as their own turns afterward. Owns the ordering, the stable
    per-app id sequence, and the paused flag; the App performs the effects
    (panel repaint, draining a popped item into a turn worker). Free of Textual
    so the queue logic is unit-testable without an App."""

    def __init__(self) -> None:
        self._items: list[QueuedMessage] = []
        # Monotonic across enqueue AND prepend so a re-inserted steer never
        # collides with a pending item's id — the panel targets items by id.
        self._seq = 0
        # Flipped by the App on cancel/error so a drained turn waits for an
        # explicit resume; lives here because every queue read needs it.
        self.paused = False

    @property
    def items(self) -> list[QueuedMessage]:
        return self._items

    def __bool__(self) -> bool:
        return bool(self._items)

    def enqueue(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Buffer a submission to run after the current turn."""
        self._seq += 1
        self._items.append(QueuedMessage(text, attachments, str(self._seq)))

    def prepend(
        self, text: str, attachments: list[tuple[bytes, str]] | None = None
    ) -> None:
        """Re-insert a submission at the FRONT so it runs next — used for steers
        that landed in the turn-finishing gap and fall back to the queue."""
        self._seq += 1
        self._items.insert(0, QueuedMessage(text, attachments, str(self._seq)))

    def pop_next(self) -> QueuedMessage:
        """Remove and return the front item (the next to run)."""
        return self._items.pop(0)

    def remove(self, id: str) -> None:
        """Drop a pending item by id; a no-op if the id is absent."""
        self._items = [m for m in self._items if m.id != id]

    def take(self, id: str) -> QueuedMessage | None:
        """Pop a specific item out of the queue and return it, or None if the id
        is not present (used to load a queued message back into the prompt)."""
        item = next((m for m in self._items if m.id == id), None)
        if item is not None:
            self._items = [m for m in self._items if m.id != id]
        return item
