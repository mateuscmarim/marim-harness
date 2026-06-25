"""Cursor + spend formatting for the full-screen sub-agent viewer (ctrl+x).

Holds only the open/index state and the pure spend-tag formatter; the App owns
every widget effect (mount, display toggling, focus, ``stream.viewing_sid``).
Free of Textual so the clamp/step arithmetic and the tag formatting are
unit-testable without an App."""

from __future__ import annotations

from .widgets.format import human_tokens


def spend_tag(tokens: int, max_ctx: int) -> str:
    """A compact ``{tokens} ({pct}%)`` spend tag for the footer, where pct is the
    share of the model's context window. Empty until the spawn is metered; drops
    the percentage when the context size is unknown."""
    if not tokens:
        return ""
    tag = human_tokens(tokens)
    if max_ctx:
        tag += f" ({round(tokens / max_ctx * 100)}%)"
    return tag


class SubAgentViewer:
    """The viewer's cursor: whether it's open and which spawn is selected. The
    App reads/sets these and performs all the widget effects around them."""

    def __init__(self) -> None:
        self.open = False
        self.index = 0

    def clamp(self, count: int) -> int:
        """Pin the index into ``[0, count-1]`` and return it."""
        self.index = max(0, min(self.index, count - 1))
        return self.index

    def prev(self) -> None:
        self.index -= 1

    def next(self) -> None:
        self.index += 1
