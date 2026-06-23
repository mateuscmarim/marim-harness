"""Simple log-line widgets: user/error/notice/turn-meta messages, the compaction
summary block, and the streaming assistant markdown.

The text-carrying Static subclasses use ``markup=False`` because their content is
arbitrary text — user input, exception strings, MCP errors, model prose — that may
contain Rich markup syntax (e.g. a stray ``[/]``). Their glyph and colour come from
CSS classes, not inline markup, so an unescaped bracket can't raise a MarkupError
that crashes the app during layout.
"""

from textual.content import Content
from textual.widgets import Collapsible, Markdown, Static


class UserMessage(Static):
    def __init__(self, text: str) -> None:
        super().__init__(f"› {text}", classes="user-msg", markup=False)


class ErrorMessage(Static):
    """A turn that failed: shown in the log so the session survives the error."""

    def __init__(self, text: str) -> None:
        super().__init__(f"✕ {text}", classes="error-msg", markup=False)


class NoticeMessage(Static):
    """A low-key system note in the log (e.g. history was compacted)."""

    def __init__(self, text: str) -> None:
        super().__init__(f"· {text}", classes="notice-msg", markup=False)


class SummaryWidget(Collapsible):
    """A compaction summary shown as a distinct, collapsed block in the log so it
    reads as condensed earlier context — not as something the user typed. The body
    is the summary text; markup=False because it is untrusted model output."""

    def __init__(self, summary_body: str) -> None:
        # markup=False: the summary is model-generated prose that may contain
        # bracket sequences Rich would otherwise try (and fail) to parse.
        self._body = Static(summary_body, markup=False)
        # A literal Content title bypasses Textual's markup parsing, matching the
        # other Collapsible titles in this module.
        super().__init__(
            self._body, title=Content("≡ Conversation summary"), collapsed=True  # pyright: ignore[reportArgumentType]
        )


class ThinkingWidget(Collapsible):
    """A model's chain-of-thought, shown as a distinct collapsed block so it's
    available on demand without cluttering the reply. The body is a streaming
    AssistantMessage (Markdown); because it lives inside a collapsed Collapsible,
    the flush tick defers its (re)render until the user expands it — the same
    deferral the folded sub-agent bodies rely on."""

    def __init__(self) -> None:
        self.body = AssistantMessage()
        super().__init__(
            self.body, title=Content("✦ thinking"), collapsed=True  # pyright: ignore[reportArgumentType]
        )


class TurnMeta(Static):
    """A dim per-turn footer stamped under a reply — e.g. how long the turn took."""

    def __init__(self, text: str) -> None:
        super().__init__(f"· {text}", classes="turn-meta", markup=False)


class AssistantMessage(Markdown):
    """Streaming assistant text rendered as Markdown. ``append`` only buffers the
    delta — re-parsing the whole markdown document on every token is O(n²) and
    makes streaming janky — so the (expensive) render is deferred to ``flush``,
    which the app drives on a shared interval to coalesce many deltas into one
    parse."""

    def __init__(self) -> None:
        self.text = ""
        self._pending = False
        super().__init__("")

    def append(self, delta: str) -> None:  # type: ignore[override]
        self.text += delta
        self._pending = True

    def flush(self) -> bool:
        """Render the buffered text if there is any. Returns whether it rendered,
        so the caller can skip scroll/update work when nothing changed."""
        if not self._pending:
            return False
        self.update(self.text)
        self._pending = False
        return True
