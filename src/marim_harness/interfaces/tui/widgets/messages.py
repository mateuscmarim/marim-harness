"""Simple log-line widgets: user/error/notice/turn-meta messages, the compaction
summary block, and the streaming assistant markdown.

The text-carrying Static subclasses use ``markup=False`` because their content is
arbitrary text — user input, exception strings, MCP errors, model prose — that may
contain Rich markup syntax (e.g. a stray ``[/]``). Their glyph and colour come from
CSS classes, not inline markup, so an unescaped bracket can't raise a MarkupError
that crashes the app during layout.
"""

from textual.app import ComposeResult
from textual.containers import Vertical
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


# While streaming, completed lines are frozen into immutable child Statics in
# batches of this many lines; only the small live tail is re-rendered each flush.
_FREEZE_EVERY = 24


def _nth_newline(text: str, n: int) -> "int | None":
    """Index of the ``n``-th ``\\n`` in ``text``, or ``None`` if there are fewer
    than ``n``. Used to find how much of the live tail is complete enough to
    freeze (a line is only complete once a newline follows it)."""
    idx = -1
    for _ in range(n):
        idx = text.find("\n", idx + 1)
        if idx == -1:
            return None
    return idx


class ThinkingWidget(Vertical):
    """A model's chain-of-thought, shown inline behind an accent rail with an
    italic ``Thinking:`` label — distinct from the reply but read at a glance, no
    expand needed. Reasoning is rendered as plain styled text (not Markdown): it's
    conversational prose where markdown structure rarely matters. ``self.body =
    self`` keeps the streaming interface the renderer drives (``append``/``flush``
    on ``widget.body``) pointed at this widget.

    **Incremental streaming.** A single Static reflows its *whole* content on every
    update, so streaming a long thought through one would be O(n²) over the turn.
    Instead the widget is a column of child Statics: as completed lines pile up they
    are *frozen* into immutable child Statics (rendered once, never touched again) in
    batches of ``_FREEZE_EVERY``, while a small ``_live`` Static holds only the
    current unfrozen tail. So each flush re-renders only the bounded tail — O(delta),
    not O(whole thought) — while the user still sees the full thought form live (the
    frozen children stay on screen and remain scrollable).

    A thought streams in *full*; once it finishes (``finalize``) it collapses to its
    last ``_THINKING_CAP`` lines behind a dim ``… +N more lines (ctrl+o)`` header, so
    a long deliberation doesn't bury the reply. ``finalize`` drops the frozen
    children and renders the capped view (``_render``) into ``_live``; the app's
    Ctrl+O reveal-all (``set_reveal``) re-renders it expanded in place.

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
        # The themed label, built once (it resolves $text-accent at render time).
        self._label = Content.from_markup("[$text-accent]Thinking:[/] ")
        # How many chars of self.text have been frozen into immutable child Statics;
        # self.text[_frozen_chars:] is the live tail shown in _live.
        self._frozen_chars = 0
        self._frozen: list[Static] = []
        self._live = Static(markup=False)
        super().__init__(classes="thinking")

    def compose(self) -> ComposeResult:
        yield self._live

    def append(self, delta: str) -> None:
        self.text += delta
        self._pending = True

    def flush(self) -> bool:
        """Render the reasoning buffered since the last flush. Returns whether it
        rendered, so the flush tick can skip idle streams.

        Completed lines beyond the live window are frozen into child Statics (which
        are never re-rendered after); only the bounded live tail is re-rendered, so
        the work per flush is proportional to the new delta, not the whole thought."""
        # After finalize the capped view is static; nothing more to stream. Before
        # mount, keep the delta buffered (mounting frozen chunks needs the anchor).
        if self._done or not self._pending or not self.is_mounted:
            return False
        self._pending = False
        live = self.text[self._frozen_chars :]
        while (nl := _nth_newline(live, _FREEZE_EVERY)) is not None:
            chunk = live[: nl + 1]
            self._freeze_chunk(chunk, first=self._frozen_chars == 0)
            self._frozen_chars += len(chunk)
            live = self.text[self._frozen_chars :]
        body = Content(live)
        self._live.update(self._label + body if self._frozen_chars == 0 else body)
        return True

    def _freeze_chunk(self, chunk: str, first: bool) -> None:
        """Mount the completed ``chunk`` as an immutable child Static above the live
        tail. The first chunk carries the label so it stays pinned at the top."""
        body = Content(chunk.rstrip("\n"))
        content = self._label + body if first else body
        frozen = Static(content, markup=False)
        self._frozen.append(frozen)
        self.mount(frozen, before=self._live)

    def finalize(self) -> None:
        """Mark the reasoning stream complete so the inline view caps to its last
        lines. Called once when the thought ends — the next part starts or the run's
        event stream drains. Idempotent: re-finalizing is a no-op. Drops the frozen
        chunks and renders the capped view into the (now sole) live Static."""
        if self._done:
            return
        self._done = True
        for frozen in self._frozen:
            frozen.remove()
        self._frozen.clear()
        self._frozen_chars = 0
        self._live.update(self._render())

    def set_reveal(self, value: bool) -> None:
        """Ctrl+O reveal-all: show the whole thought, or restore the capped preview
        on a second press. Only meaningful once finished — while streaming the full
        text is already on screen, so this just records the flag for finalize."""
        self.reveal = value
        if self._done:
            self._live.update(self._render())

    def _render(self) -> Content:
        # The label carries its own themed colour via markup; the reasoning text
        # is appended as a literal Content (no markup parse) and inherits the
        # italic/muted styling the .tcss puts on the widget. While streaming (or
        # when revealed) the full text shows; a finished thought caps to its tail.
        if not self._done or self.reveal:
            return self._label + Content(self.text)
        kept, hidden = _cap_tail(self.text, _THINKING_CAP)
        if not hidden:
            return self._label + Content(kept)
        header = Content.from_markup(f"[dim]… +{hidden} more lines (ctrl+o)[/]\n")
        return self._label + header + Content(kept)


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
    parse.

    ``flush`` renders *incrementally*: it feeds only the text added since the last
    flush to ``Markdown.append`` (the base class), which re-parses just the trailing
    (still-open) block and advances its parse cursor past completed ones. So the work
    per flush is proportional to the new delta, not the whole accumulated reply —
    turning the per-turn cost from O(n²) back to O(n). ``self.text`` holds the full
    buffered source (the renderer reads it on replay/inspection); ``_rendered_len``
    marks how much of it has been handed to the document."""

    def __init__(self) -> None:
        self.text = ""
        self._pending = False
        # How many chars of self.text have already been parsed into the document.
        self._rendered_len = 0
        # Latch so finalize() (the stream-end clean re-render) runs at most once.
        self._finalized = False
        super().__init__("")

    def append(self, delta: str) -> None:  # type: ignore[override]
        self.text += delta
        self._pending = True

    def flush(self) -> bool:
        """Render the text buffered since the last flush. Returns whether it
        rendered, so the caller can skip scroll/update work when nothing changed.

        Only the new tail is parsed: Markdown.append re-parses from its last parsed
        line, keeping each flush proportional to the delta rather than the whole
        document."""
        # Don't render before the widget is attached: Markdown.append mounts its
        # parsed blocks as children, which raises MountError off the DOM. Keep the
        # delta buffered (stay _pending) so the first flush after mount renders it.
        # The live/replay paths always mount before the first append_stream, so this
        # only guards stray ticks; ThinkingWidget.flush guards the same way.
        if not self._pending or not self.is_mounted:
            return False
        delta = self.text[self._rendered_len :]
        self._pending = False
        if not delta:
            return False
        self._rendered_len = len(self.text)
        # Markdown.append (the base method this class shadows) takes only the new
        # fragment and appends it to the live document. Fire-and-forget like the
        # update() it replaces: the returned AwaitComplete self-schedules.
        super().append(delta)
        return True

    def finalize(self) -> None:
        """Re-render the whole buffered source once the stream ends, collapsing any
        block duplication the incremental path can leave behind.

        Textual's ``Markdown.append`` keeps the trailing block "open" and re-parses
        it on each call, computing its splice point from ``_last_parsed_line`` in the
        *synchronous* prefix of an otherwise async (``AwaitComplete``) update. When
        flush ticks fire faster than those updates drain — a busy event loop during a
        sub-agent fan-out is the usual trigger — successive appends capture stale
        parse state and re-mount blocks that are already on screen, so a styled line
        (heading, bold) shows up two or more times. The raw history is fine; only the
        live incremental render doubles, which is why a reloaded session shows it once.

        Calling ``Markdown.update`` with the full buffered source drops every existing
        block and reparses from scratch (resetting ``_last_parsed_line``), so the
        finished message matches a clean parse regardless of what streaming left
        behind. One reparse per message at completion is the same cost the streaming
        path was built to defer — paid once, at the end. Idempotent via ``_finalized``;
        a no-op when nothing was ever streamed."""
        if self._finalized:
            return
        self._finalized = True
        if self._rendered_len == 0 or not self.text or not self.is_mounted:
            # Nothing to heal, or nowhere to render. A widget that never rendered
            # incrementally (_rendered_len == 0) — an off-screen sub-agent transcript
            # whose flushes were deferred, or a message that completed inside one tick
            # — has no duplication: its single pending flush parses the whole buffer in
            # one append, which can't double, and leaving it dirty preserves the
            # off-screen deferral. (Duplication needs *overlapping* appends, i.e. ≥2
            # flushes — hence _rendered_len > 0.) An unmounted widget (removed by a
            # session reset mid-stream) can't take update()'s remount either.
            return
        # Full clean reparse: removes the (possibly duplicated) blocks and re-mounts
        # the document once from the full buffered source. update() resets the base
        # parse cursor, so our own _rendered_len bookkeeping is squared with it.
        self.update(self.text)
        self._rendered_len = len(self.text)
        self._pending = False
