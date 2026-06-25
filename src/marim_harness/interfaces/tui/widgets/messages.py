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


# Lines of a *finished* thought kept inline; the tail is kept (not the head)
# because a thought's conclusion sits at its end. Ctrl+O reveals the rest.
_THINKING_CAP = 8


class ThinkingWidget(Static):
    """A model's chain-of-thought, shown inline behind an accent rail with an
    italic ``Thinking:`` label — distinct from the reply but read at a glance, no
    expand needed. Reasoning is rendered as plain styled text (not Markdown): it's
    conversational prose where markdown structure rarely matters, and a single
    flowing Static lets the label sit inline with the first words and wrap with
    them. ``self.body = self`` keeps the streaming interface the renderer drives
    (``append``/``flush`` on ``widget.body``) pointed at this widget.

    A thought streams in *full*; once it finishes (``finalize``) it collapses to
    its last ``_THINKING_CAP`` lines behind a dim ``… +N more lines (ctrl+o)``
    header, so a long deliberation doesn't bury the reply. The app's Ctrl+O
    reveal-all toggle (``set_reveal``) expands it back to the whole text in place.

    Content is built via ``Content`` (not markup parsing) so untrusted reasoning
    text can't raise a MarkupError — only the fixed label/header are markup-parsed."""

    def __init__(self) -> None:
        self.text = ""
        self._pending = False
        # The cap only applies once the stream ends; while ``_done`` is False the
        # full thought streams so the user sees it form in real time.
        self._done = False
        # Ctrl+O override: show the full text even after the thought finished.
        self.reveal = False
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

    def finalize(self) -> None:
        """Mark the reasoning stream complete so the inline view caps to its last
        lines. Called once when the thought ends — the next part starts or the
        run's event stream drains. Idempotent: re-finalizing is a no-op."""
        if self._done:
            return
        self._done = True
        self.update(self._render())

    def set_reveal(self, value: bool) -> None:
        """Ctrl+O reveal-all: show the whole thought, or restore the capped
        preview on a second press. A short or still-streaming thought renders the
        same either way, so this only visibly changes a finished, over-cap one."""
        self.reveal = value
        self.update(self._render())

    def _render(self) -> Content:
        # The label carries its own themed colour via markup; the reasoning text
        # is appended as a literal Content (no markup parse) and inherits the
        # italic/muted styling the .tcss puts on the widget. While streaming (or
        # when revealed) the full text shows; a finished thought caps to its tail.
        label = Content.from_markup("[$text-accent]Thinking:[/] ")
        if not self._done or self.reveal:
            return label + Content(self.text)
        kept, hidden = _cap_tail(self.text, _THINKING_CAP)
        if not hidden:
            return label + Content(kept)
        header = Content.from_markup(f"[dim]… +{hidden} more lines (ctrl+o)[/]\n")
        return label + header + Content(kept)


def _cap_tail(text: str, cap: int) -> "tuple[str, int]":
    """Keep the last ``cap`` lines of ``text``, returning ``(kept, hidden)`` where
    ``hidden`` is how many leading lines were dropped (0 if it fit). Pure."""
    lines = text.split("\n")
    if len(lines) <= cap:
        return text, 0
    return "\n".join(lines[-cap:]), len(lines) - cap


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
