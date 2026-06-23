"""The TUI message queue: messages the user submitted while a turn was running,
held to run as their own turns after the current one. In-memory, process-scoped."""

from dataclasses import dataclass
from typing import Optional

from textual.markup import escape


@dataclass
class QueuedMessage:
    """One buffered user submission. ``attachments`` mirrors the tuple list
    ``Harness.run_turn`` accepts; ``id`` is a stable, per-app sequence string
    used to target the item from the panel's controls."""

    text: str
    attachments: Optional[list[tuple[bytes, str]]]
    id: str


def render_queue(items: list) -> str:
    """Render the pending items as a numbered Textual-markup string. User text
    is escaped so brackets in a prompt are not parsed as markup."""
    lines = []
    for i, m in enumerate(items, 1):
        n = len(m.attachments or [])
        tag = f" 📎{n}" if n else ""
        lines.append(f"{i}. {escape(m.text)}{tag}")
    return "\n".join(lines)
