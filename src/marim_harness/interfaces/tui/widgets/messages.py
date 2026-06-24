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


class ThinkingWidget(Static):
    """A model's chain-of-thought, shown inline behind an accent rail with an
    italic ``Thinking:`` label — distinct from the reply but read at a glance, no
    expand needed. Reasoning is rendered as plain styled text (not Markdown): it's
    conversational prose where markdown structure rarely matters, and a single
    flowing Static lets the label sit inline with the first words and wrap with
    them. ``self.body = self`` keeps the streaming interface the renderer drives
    (``append``/``flush`` on ``widget.body``) pointed at this widget.

    Content is built via ``Content`` (not markup parsing) so untrusted reasoning
    text can't raise a MarkupError — only the fixed label is markup-parsed."""

    def __init__(self) -> None:
        self.text = ""
        self._pending = False
        self.body = self
        super().__init__(classes="thinking")

    def append(self, delta: str) -> None:
        self.text += delta
        self._pending = True

    def flush(self) -> bool:
        """Render the buffered reasoning if it changed. Returns whether it
        rendered so the flush tick can skip idle streams."""
        if not self._pending:
            return False
        self.update(self._render())
        self._pending = False
        return True

    def _render(self) -> Content:
        # The label carries its own themed colour via markup; the reasoning text
        # is appended as a literal Content (no markup parse) and inherits the
        # italic/muted styling the .tcss puts on the widget.
        return Content.from_markup("[$text-accent]Thinking:[/] ") + Content(self.text)


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
